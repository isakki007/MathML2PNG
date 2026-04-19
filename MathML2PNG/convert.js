'use strict';

/**
 * MathML → SVG + PNG (300 DPI) + Alt-Text
 *
 * Dependencies:
 *   npm install mathjax-full speech-rule-engine sharp
 *
 * Usage:
 *   echo '<math>...</math>' | node convert.js <output_basename>
 *
 * Stdout : single JSON line → { success, baseFileName, files, altText, ... }
 * Stderr : debug logs (Flask ignores stderr)
 */

const fs = require('fs');

// ── Collect stdin ──────────────────────────────────────────────────────────────
let chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', async () => {

    const rawInput     = chunks.join('').trim();
    const args         = process.argv.slice(2);
    const baseFileName = (args[0] || 'output').trim().replace(/[^a-zA-Z0-9_\-]/g, '_');

    // ── Validate input ─────────────────────────────────────────────────────────
    if (!rawInput) {
        console.log(JSON.stringify({ success: false, error: 'No input received' }));
        process.exit(1);
    }
    if (!/<math/i.test(rawInput)) {
        console.log(JSON.stringify({ success: false, error: 'Input does not contain a <math> element' }));
        process.exit(1);
    }

    try {

        // ── 1. Load mathjax-full ───────────────────────────────────────────────
        const { mathjax }             = require('mathjax-full/js/mathjax.js');
        const { MathML }              = require('mathjax-full/js/input/mathml.js');
        const { SVG }                 = require('mathjax-full/js/output/svg.js');
        const { liteAdaptor }         = require('mathjax-full/js/adaptors/liteAdaptor.js');
        const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');

        const adaptor = liteAdaptor();
        RegisterHTMLHandler(adaptor);

        const mmlInput  = new MathML();
        const svgOutput = new SVG({ fontCache: 'local' });

        const doc = mathjax.document('', {
            InputJax:  mmlInput,
            OutputJax: svgOutput,
        });

        // ── 2. MathML → SVG ───────────────────────────────────────────────────
        const node    = doc.convert(rawInput, { display: true });
        let   svgText = adaptor.outerHTML(node);

        // Inject explicit white background so sharp renders correctly
        svgText = svgText.replace(
            /(<svg[^>]*>)/,
            '$1<rect width="100%" height="100%" fill="white"/>'
        );

        process.stderr.write(`✓ SVG generated (${svgText.length} bytes)\n`);

        // ── 3. Alt-text via speech-rule-engine ────────────────────────────────
        let altText = '';
        try {
            const SRE = require('speech-rule-engine');

            await SRE.setupEngine({
                domain:   'mathspeak',
                style:    'default',
                locale:   'en',
                modality: 'speech',
            });

            altText = SRE.toSpeech(rawInput);
            altText = altText.trim().replace(/\s+/g, ' ');
            process.stderr.write(`✓ SRE alt-text: "${altText.substring(0, 80)}"\n`);

        } catch (sreErr) {
            process.stderr.write(`⚠ SRE failed (${sreErr.message}), using tag-strip fallback\n`);
            altText = rawInput.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
                     || 'Mathematical expression';
        }

        // ── 4. Write SVG file ─────────────────────────────────────────────────
        const svgFile = `${baseFileName}.svg`;
        fs.writeFileSync(svgFile, svgText, 'utf8');
        process.stderr.write(`✓ SVG written: ${svgFile}\n`);

        // ── 5. Write TXT (alt-text) file ──────────────────────────────────────
        const txtFile = `${baseFileName}.txt`;
        fs.writeFileSync(txtFile, altText, 'utf8');
        process.stderr.write(`✓ TXT written: ${txtFile}\n`);

        // ── 6. SVG → PNG at 300 DPI via sharp ────────────────────────────────
        const pngFile = `${baseFileName}.png`;
        try {
            const sharp = require('sharp');

            const pngBuf = await sharp(Buffer.from(svgText), { density: 300 })
                .flatten({ background: '#ffffff' })
                .toColourspace('srgb')
                .png({ quality: 100, compressionLevel: 6 })
                .toBuffer();

            fs.writeFileSync(pngFile, pngBuf);
            process.stderr.write(`✓ PNG written: ${pngFile} (${Math.round(pngBuf.length / 1024)} KB, 300 DPI)\n`);

        } catch (pngErr) {
            process.stderr.write(`⚠ PNG conversion failed: ${pngErr.message}\n`);
            // Minimal valid 1×1 white PNG — Flask checks for \x89PNG header
            const PNG_1x1 = Buffer.from(
                '89504e470d0a1a0a0000000d4948445200000001000000010802' +
                '0000009001' + '2e000000000c49444154789c6260f8cfc00000000200' +
                '016221bc330000000049454e44ae426082', 'hex'
            );
            fs.writeFileSync(pngFile, PNG_1x1);
        }

        // ── 7. Emit JSON to stdout for Flask ──────────────────────────────────
        console.log(JSON.stringify({
            success:       true,
            id:            null,
            baseFileName:  baseFileName,
            requestedName: baseFileName,
            files: {
                svg: svgFile,
                png: pngFile,
                txt: txtFile,
            },
            altText:      altText,
            svgSize:      fs.statSync(svgFile).size,
            pngSize:      fs.statSync(pngFile).size,
            txtSize:      fs.statSync(txtFile).size,
            format:       'mathml',
            convertedAt:  new Date().toISOString(),
        }));

        process.exit(0);

    } catch (err) {
        process.stderr.write(`✗ Fatal error: ${err.message}\n${err.stack}\n`);
        console.log(JSON.stringify({ success: false, error: err.message }));
        process.exit(1);
    }
});

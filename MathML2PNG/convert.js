'use strict';

const fs   = require('fs');
const path = require('path');
const sharp = require('sharp');
const SRE = require('speech-rule-engine');
const mj = require("mathjax-node-sre");

// MathJax configuration
mj.config({
MathJax: {
jax: ["input/MathML", "output/SVG"],
"HTML-CSS": {
availableFonts: ["Cambria Math", "TeX", "STIX"],
webFont: "Cambria Math",
imageFont: "STIX"
     }
         },
sre: {
speech: "shallow",
domain: "mathspeak",
style: "default",
locale: "en"
         }
});
mj.start();

// --- Collect input ---
let chunks = [];
process.stdin.on('data', chunk => chunks.push(chunk));
process.stdin.on('end', async () => {
    const rawInput = chunks.join('').trim();
    const args = process.argv.slice(2);
    const baseFileName = (args[0] || 'output').trim().replace(/[^a-zA-Z0-9_\-]/g, '_');

    if (!rawInput) {
        console.log(JSON.stringify({ success: false, error: 'No input received' }));
        process.exit(1);
    }

    // --- Validate MathML ---
    if (!rawInput.startsWith("<math")) {
        console.error("Invalid MathML input");
        console.log(JSON.stringify({ success: false, error: "Invalid MathML input" }));
        process.exit(1);
    }

    // --- MathJax conversion to SVG ---
    let svgText = '';
    try {
        const result = await new Promise((resolve, reject) => {
            mj.typeset({
math: rawInput,
format: "MathML",
svg: true,
speakText: true
            }, res => {
                if (res.errors) reject(res.errors);
                else resolve(res);
            });
        });

        svgText = result.svg;

        // Ensure white background for PNG conversion (Sharp requires it)
        svgText = svgText.replace(
                                  /(<svg[^>]*>)/,
                                  '$1<rect width="100%" height="100%" fill="white"/>'
                                 );

        // --- Alt-text generation using SRE ---
        let altText = '';
        try {
            await SRE.setupEngine({
domain: 'mathspeak',
style: 'default',
locale: 'en',
modality: 'speech',
            });
            altText = SRE.toSpeech(rawInput);
            process.stderr.write(`?? SRE alt-text: "${altText.substring(0, 80)}"\n`);
        } catch (sreErr) {
            process.stderr.write(`?? SRE failed (${sreErr.message}), using fallback\n`);
            // Fallback: extract text content from MathML tags
            const textContent = rawInput
                                .replace(/<[^>]+>/g, ' ')  // Remove all HTML tags
                                .replace(/\s+/g, ' ')      // Normalize whitespace
                                .trim();
            altText = textContent || 'Mathematical expression';
        }

        // --- Write SVG ---
        const svgFile = `${baseFileName}.svg`;
        fs.writeFileSync(svgFile, svgText, 'utf8');
        process.stderr.write(`?? SVG: ${svgFile} (${svgText.length} bytes)\n`);

        // --- Write Alt-text (TXT) ---
        const txtFile = `${baseFileName}.txt`;
        const altClean = altText.trim().replace(/\s+/g, ' ');
        fs.writeFileSync(txtFile, altClean, 'utf8');
        process.stderr.write(`?? TXT: ${txtFile}\n`);

        // --- Convert SVG to PNG with Sharp (300 DPI) ---
        const pngFile = `${baseFileName}.png`;
        try {
            const pngBuf = await sharp(Buffer.from(svgText), {
density: 300,              // 300 DPI
            })
                           .flatten({ background: '#ffffff' })
                 .toColourspace('srgb')
                 .png({ quality: 100, compressionLevel: 6 })
                 .toBuffer();

         fs.writeFileSync(pngFile, pngBuf);
         process.stderr.write(`?? PNG: ${pngFile} (${Math.round(pngBuf.length / 1024)} KB, 300 DPI)\n`);
        } catch (pngErr) {
            process.stderr.write(`?? PNG failed: ${pngErr.message}\n`);
            // Write minimal valid 1x1 white PNG so Flask doesn't crash
            const PNG_FALLBACK = Buffer.from(
                                             '89504e470d0a1a0a0000000d494844520000000100000001080200000090012e000000000c49444154789c6260f8cfc00000000200016221bc330000000049454e44ae426082', 'hex'
                                            );
            fs.writeFileSync(pngFile, PNG_FALLBACK);
        }

        // --- Output JSON ---
        const svgSize = fs.statSync(svgFile).size;
        const pngSize = fs.statSync(pngFile).size;
        const txtSize = fs.statSync(txtFile).size;

        const output = {
success: true,
id: null,
baseFileName: baseFileName,
requestedName: baseFileName,
files: {
svg: svgFile,
png: pngFile,
txt: txtFile
         },
altText: altClean,
svgSize: svgSize,
pngSize: pngSize,
txtSize: txtSize,
format: "mathml",
convertedAt: new Date().toISOString()  // Include timestamp
        };

        console.log(JSON.stringify(output));

    } catch (err) {
        process.stderr.write(`? Fatal: ${err.message}\n${err.stack}\n`);
        console.log(JSON.stringify({ success: false, error: err.message }));
        process.exit(1);
    }
});
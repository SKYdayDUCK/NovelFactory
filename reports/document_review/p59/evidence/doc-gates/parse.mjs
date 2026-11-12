import fs from 'node:fs';
import { JSDOM } from 'jsdom';
const dom = new JSDOM('');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
const { default: mermaid } = await import('mermaid');
let count = 0;
for (const file of process.argv.slice(2)) {
  const source = fs.readFileSync(file,'utf8');
  const blocks = file.endsWith('.mmd') ? [source] : [...source.matchAll(/^```mermaid[ \t]*\r?\n([\s\S]*?)^```[ \t]*\r?$/gm)].map(m => m[1]);
  for (const [index,block] of blocks.entries()) {
    try {
      const info = await mermaid.parse(block);
      console.log(JSON.stringify({file,block:index+1,type:info.diagramType,status:"PASS"}));
      count++;
    } catch (error) {
      console.error(JSON.stringify({file,block:index+1,error:String(error.message)}));
      process.exitCode = 1;
    }
  }
}
console.log(JSON.stringify({passed:count}));

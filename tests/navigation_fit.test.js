const fs=require('fs'),assert=require('assert');
const source=fs.readFileSync('navigation_fit.js','utf8');
assert(source.includes('height:100dvh!important'));
assert(source.includes('overflow-y:auto'));
assert(source.includes("innerHeight<820||visible>8"));
assert(source.includes('@media(max-width:900px)'));
assert(source.includes("closeOtherMenus"));
assert(source.includes('aria-current'));
assert(source.includes('syncActive(nav)'));
assert(source.includes('inset 4px 0 0 #e7c684'));
console.log('navigation fit checks passed');


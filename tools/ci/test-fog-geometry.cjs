#!/usr/bin/env node
// Stored fog cells are world-space data. Exercise the actual browser projection
// functions at non-central positions, independently of the map texture size.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const filename = process.argv[2] || path.join(__dirname, '../../src/ValheimOne/LiveMap/web/app.js');
const source = fs.readFileSync(filename, 'utf8');
function extract(name) {
    const start = source.indexOf('    function ' + name + '(');
    if (start < 0) return '';
    const end = source.indexOf('\n    function ', start + 1);
    return source.slice(start, end < 0 ? source.length : end);
}
function bounds(a, b) {
    const south = Math.min(a.lat, b.lat), north = Math.max(a.lat, b.lat);
    const west = Math.min(a.lng, b.lng), east = Math.max(a.lng, b.lng);
    return {getNorthWest: () => ({lat:north,lng:west}), getSouthEast: () => ({lat:south,lng:east})};
}
let draw;
const context2d = () => ({createImageData: (w,h) => ({data:new Uint8ClampedArray(w*h*4)}),
    putImageData(){},setTransform(){},clearRect(){},drawImage(...args){draw=args;}});
const canvas = () => ({width:0,height:0,style:{},getContext:context2d});
const sandbox = {console,Math,Number,Uint8ClampedArray,DEFAULT_FOG_WORLD_SPAN:24576,
    fogStatus:{worldSpan:24576},worldBounds:bounds({lat:-256,lng:0},{lat:0,lng:256}),
    minimapFogImage:{style:{}},document:{createElement:canvas},window:{devicePixelRatio:1},
    L:{latLng:(lat,lng)=>({lat,lng}),latLngBounds:bounds,DomUtil:{setPosition(){}},
        Layer:{extend(methods){function Layer(){this.initialize();}Object.assign(Layer.prototype,methods);return Layer;}}}};
vm.createContext(sandbox);
for (const name of ['worldToLatLng','latLngToWorld','fogWorldBounds','updateMinimapFogBounds','createTimelapseFogLayer']) {
    const code=extract(name);
    if(code)vm.runInContext(code,sandbox);
}
// An old build draws the whole canonical fog image over worldBounds. Retaining
// that path here proves the old behavior fails numerically, not just by a missing helper.
if(!sandbox.fogWorldBounds)sandbox.fogWorldBounds=()=>sandbox.worldBounds;
if(!sandbox.updateMinimapFogBounds)sandbox.updateMinimapFogBounds=()=>Object.assign(sandbox.minimapFogImage.style,{left:'0%',top:'0%',width:'100%',height:'100%'});
const points=[{column:285,row:269},{column:128,row:159},{column:450,row:105}];
const failures=[];
function near(actual,expected,label){if(Math.abs(actual-expected)>.0001)failures.push(`${label}: ${actual} != ${expected}`);}
for(const textureSize of [512,1024,2048,4096]) {
    sandbox.mapMetrics={textureSize,pixelSize:12,unitsPerPixel:256/textureSize};
    const extent=sandbox.fogWorldBounds(),nw=extent.getNorthWest(),se=extent.getSouthEast();
    sandbox.updateMinimapFogBounds();
    const style=sandbox.minimapFogImage.style;
    const layer=sandbox.createTimelapseFogLayer();
    layer._map={getSize:()=>({x:640,y:640}),containerPointToLayerPoint:p=>p,
        latLngToContainerPoint:p=>({x:p.lng*2,y:-p.lat*2})};
    layer._canvas=canvas();
    for(const p of points) {
        // The server's 512-cell mask has 48-metre cells over 24,576 metres.
        const x=(p.column+.5)*48-12288,z=12288-(p.row+.5)*48;
        const u=(p.column+.5)/512,v=(p.row+.5)/512;
        const shown=sandbox.latLngToWorld({lng:nw.lng+u*(se.lng-nw.lng),lat:nw.lat+v*(se.lat-nw.lat)});
        near(shown.x,x,`live ${textureSize} x`);near(shown.z,z,`live ${textureSize} z`);
        const mini=sandbox.latLngToWorld({lng:(parseFloat(style.left)/100+u*parseFloat(style.width)/100)*256,
            lat:-(parseFloat(style.top)/100+v*parseFloat(style.height)/100)*256});
        near(mini.x,x,`minimap ${textureSize} x`);near(mini.z,z,`minimap ${textureSize} z`);
        const index=p.row*512+p.column;
        layer.setData([index,1,512*512-index-1]);
        const replay=sandbox.latLngToWorld({lng:(draw[1]+u*draw[3])/2,lat:-(draw[2]+v*draw[4])/2});
        near(replay.x,x,`history ${textureSize} x`);near(replay.z,z,`history ${textureSize} z`);
        assert.equal(layer._imageData.data[index*4+3],0,'Explored cell stays transparent');
        assert.equal(layer._imageData.data[(index+1)*4+3],209,'Adjacent unexplored cell stays covered');
    }
}
if(failures.length){console.error(failures.join('\n'));process.exit(1);}
assert.match(source,/L\.imageOverlay\(url, fogWorldBounds\(\)/,'Live overlay must use the tested bounds');
console.log('Fog geometry passed: live map, minimap and history at 512/1024/2048/4096, three off-center cells each.');

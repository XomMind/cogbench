// Exercise the shipped player code with a deterministic SourceBuffer double.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../stream/watch.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
const get = id => {
  if (!nodes.has(id)) nodes.set(id, {
    classList: {add(){}, remove(){}, toggle(){}}, addEventListener(){},
    currentTime: 10, play: () => Promise.resolve(),
  });
  return nodes.get(id);
};
const timers = [];
const context = vm.createContext({
  window: {}, document: {getElementById:get, body:get('body')},
  location: {protocol:'http:', hostname:'localhost', reload(){}},
  addEventListener(){}, setInterval(){}, clearTimeout(){},
  setTimeout: fn => timers.push(fn), Uint8Array,
  fetch: async () => { throw new Error('offline'); },
});
vm.runInContext(script, context);
vm.runInContext(`
  let removed = null;
  sb = {updating:false, buffered:{length:1, start:()=>0},
        remove:(from,to)=>{removed=[from,to]; sb.updating=true;},
        appendBuffer:()=>{throw {name:'QuotaExceededError',message:'full'};}};
  queue = [new Uint8Array([1,2,3])];
  pump();
`, context);
assert.equal(vm.runInContext('queue.length', context), 1, 'quota recovery retains data');
assert.equal(vm.runInContext('queue[0][2]', context), 3);
assert.equal(vm.runInContext('removed[1]', context), 8);
vm.runInContext(`
  sb.updating=false;
  sb.appendBuffer=chunk=>{globalThis.received=Array.from(chunk);};
  pump();
`, context);
assert.equal(vm.runInContext('queue.length', context), 0);
assert.equal(vm.runInContext('received.join(",")', context), '1,2,3');
vm.runInContext('fail("<img src=x onerror=bad()>")', context);
assert.equal(get('msgt').textContent, '<img src=x onerror=bad()>');
const before = timers.length;
vm.runInContext('reconnect("broken"); reconnect("again")', context);
assert.equal(timers.length, before + 1, 'only one reconnect is scheduled');
console.log('Player checks passed: quota retry, intact bytes, safe error text, reconnect.');

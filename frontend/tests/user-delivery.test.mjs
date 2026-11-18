import test,{afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {createUserDelivery} from '../src/chat/user-delivery.js';
class Node {
  constructor(){this.children=[];this.dataset={};this.textContent='';}
  appendChild(node){this.children.push(node);}
  setAttribute(){}
  querySelector(){return this.children.find(node=>node.className==='msg-delivery');}
  matches(selector){return selector==='.chat-msg.user'&&this.user===true;}
}
const originals=new Map(['document','MutationObserver','Element'].map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
afterEach(()=>{for(const [key,descriptor]of originals)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];});
function fixture(){
  let saved=0;
  const first=Object.assign(new Node(),{user:true,dataset:{mid:'a'}});
  const second=Object.assign(new Node(),{user:true,dataset:{mid:'b'}});
  const ctx={state:{items:[{id:'a',type:'user',turn_id:'first',delivery:'pending'},
    {id:'b',type:'user',turn_id:'second',delivery:'pending'}]},dom:{messages:{querySelectorAll:()=>[first,second]}}};
  Object.defineProperty(globalThis,'Element',{value:Node,configurable:true});
  Object.defineProperty(globalThis,'document',{value:{createElement:()=>new Node()},configurable:true});
  Object.defineProperty(globalThis,'MutationObserver',{value:class{observe(){}},configurable:true});
  const delivery=createUserDelivery(ctx,{saveHistory:()=>{saved++;}});
  return{ctx,delivery,first,second,saved:()=>saved};
}
test('pending user input becomes read only for the acknowledged turn',()=>{
  const f=fixture();assert.equal(f.first.querySelector().textContent,'发送中…');
  f.delivery.read('first');assert.equal(f.first.querySelector().textContent,'已接收');
  assert.equal(f.second.querySelector().textContent,'发送中…');assert.equal(f.saved(),1);
});
test('network uncertainty is visible and never downgrades a received message',()=>{
  const f=fixture();f.delivery.unknown('first');assert.equal(f.first.querySelector().textContent,'未确认送达');
  f.delivery.read('first');f.delivery.unknown('first');f.delivery.read('first');
  assert.equal(f.first.querySelector().textContent,'已接收');assert.equal(f.saved(),2);
});
test('unrelated and missing receipt identities cannot acknowledge inputs',()=>{
  const f=fixture();f.delivery.read('other-conversation');f.delivery.read(null);
  assert.equal(f.first.querySelector().textContent,'发送中…');assert.equal(f.saved(),0);
});

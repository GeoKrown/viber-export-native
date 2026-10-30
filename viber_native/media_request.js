'use strict';
const cfg = __CONFIG__;
if(Process.platform!=='windows'||Process.arch!=='x64')throw Error('unsupported_platform');
const core = Process.getModuleByName('Qt6Core.dll');
const gui = Process.getModuleByName('Qt6Gui.dll');
const qml = Process.getModuleByName('Qt6Qml.dll');
function fn(m,n,r,a) {return new NativeFunction(m.getExportByName(n),r,a);}
const instance=fn(core,'?instance@QCoreApplication@@SAPEAV1@XZ','pointer',[]);
const thread=fn(core,'?thread@QObject@@QEBAPEAVQThread@@XZ','pointer',['pointer']);
const current=fn(core,'?currentThread@QThread@@SAPEAV1@XZ','pointer',[]);
const children=fn(core,'?children@QObject@@QEBAAEBV?$QList@PEAVQObject@@@@XZ','pointer',['pointer']);
const context=fn(qml,'?qmlContext@@YAPEAVQQmlContext@@PEBVQObject@@@Z','pointer',['pointer']);
const make=fn(core,'?fromUtf16@QString@@SA?AV1@PEB_S_J@Z','void',['pointer','pointer','int64']);
const freeString=fn(core,'??1QString@@QEAA@XZ','void',['pointer']);
const construct=fn(qml,'??0QQmlExpression@@QEAA@PEAVQQmlContext@@PEAVQObject@@AEBVQString@@1@Z','pointer',['pointer','pointer','pointer','pointer','pointer']);
const destroy=fn(qml,'??1QQmlExpression@@UEAA@XZ','void',['pointer']);
const evaluate=fn(qml,'?evaluate@QQmlExpression@@QEAA?AVQVariant@@PEA_N@Z','void',['pointer','pointer','pointer']);
const hasError=fn(qml,'?hasError@QQmlExpression@@QEBA_NXZ','bool',['pointer']);
const stringify=fn(core,'?toString@QVariant@@QEBA?AVQString@@XZ','void',['pointer','pointer']);
const freeVariant=fn(core,'??1QVariant@@QEAA@XZ','void',['pointer']);
function text(p) {const n=p.add(16).readS64().toNumber();if(n<0||n>16000)throw Error('text_limit');return n?p.add(8).readPointer().readUtf16String(n):'';}
function pointers(p) {const n=p.add(16).readS64().toNumber();if(n<0||n>5000)throw Error('children_limit');let a=[];for(let i=0;i<n;i++)a.push(p.add(8).readPointer().add(i*8).readPointer());return a;}
function expression(ctx,obj,source) {
 const s=Memory.alloc(24);make(s,Memory.allocUtf16String(source),source.length);
 const e=Memory.alloc(128);construct(e,ctx,obj,s,ptr(0));
 try {const v=Memory.alloc(64);evaluate(e,v,ptr(0));try {
  if(hasError(e))return {error:'qml_expression_failed'};
  const t=Memory.alloc(24);stringify(v,t);try{return {result:text(t)};}finally{freeString(t);}
 }finally{freeVariant(v);}}finally{destroy(e);freeString(s);}
}
let done=false,busy=false;
Interceptor.attach(core.getExportByName('?notifyInternal2@QCoreApplication@@CA_NPEAVQObject@@PEAVQEvent@@@Z'),{onLeave(){
 if(done||busy)return;
 const app=instance();if(app.isNull()||!thread(app).equals(current()))return;
 busy=true;
 try {
  const roots=pointers(gui.getExportByName('?window_list@QGuiApplicationPrivate@@2V?$QList@PEAVQWindow@@@@A'));
  const seen=new Set();let queue=[...roots];
  while(queue.length&&seen.size<2000){
   const obj=queue.shift();if(obj.isNull()||seen.has(obj.toString()))continue;seen.add(obj.toString());
   const ctx=context(obj);
   if(!ctx.isNull()) {
    // Only this fixed operation is evaluated; IDs were bounded by the worker.
    const code="(function(){var before=Viber.Chat.chatID; var ids="+JSON.stringify(cfg.event_ids)+"; for(var i=0;i<ids.length;i++)Viber.Chat.requestPayload(ids[i],true); return JSON.stringify({requested:ids,chat_unchanged:before===Viber.Chat.chatID});})()";
    const result=expression(ctx,obj,code);
    done=true;
    if(result.error)send({status:'error',error:'payload_command_failed'});
    else send({status:'ok',...JSON.parse(result.result)});
    return;
   }
   queue.push(...pointers(children(obj)));
  }
  done=true;send({status:'unavailable',error:'qml_context_not_found'});
 }catch(_){done=true;send({status:'error',error:'payload_command_failed'});}finally{busy=false;}
}});
send({status:'ready'});

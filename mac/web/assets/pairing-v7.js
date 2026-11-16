(function(root){
  'use strict';
  function parse(value){
    let text=String(value||'').replace(/[\u200B\uFEFF]/g,'').trim();
    if(!/^https?:\/\//i.test(text)&&/^\d+\.\d+\.\d+\.\d+(?::\d+)?\//.test(text))text='http://'+text;
    let url;try{url=new URL(text);}catch{throw Error('二维码中不是有效的网址，请对准电脑上的连接二维码。');}
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('请扫描电脑遥控器页面上的连接二维码。');
    const hostname=url.hostname.toLowerCase().replace(/^\[|\]$/g,'');
    if(hostname==='localhost'||hostname==='127.0.0.1')throw Error('这是电脑本机地址。请扫描电脑页面中以 192.168、10 或 172 开头的手机连接二维码。');
    const parts=hostname.split('.').map(Number);
    const ipv4=parts.length===4&&parts.every(n=>Number.isInteger(n)&&n>=0&&n<=255);
    const local=ipv4&&(parts[0]===10||(parts[0]===192&&parts[1]===168)||(parts[0]===172&&parts[1]>=16&&parts[1]<=31)||(parts[0]===169&&parts[1]===254));
    if(!(local||hostname.endsWith('.local')||/^(?:fd|fc)[a-f0-9:]+$/.test(hostname)))throw Error('二维码指向陌生地址，请扫描电脑上的局域网连接二维码。');
    let fragment;try{fragment=decodeURIComponent(url.hash.slice(1));}catch{fragment=url.hash.slice(1);}
    const params=new URLSearchParams(fragment.replace(/^\?/,''));
    const token=params.get('token')||params.get('key')||url.searchParams.get('token')||url.searchParams.get('key')||fragment;
    if(!/^[A-Za-z0-9_-]{20,128}$/.test(token))throw Error('这个二维码没有配对信息。请扫描电脑连接页面中的二维码，而不是浏览器地址或路由器二维码。');
    url.pathname='/app/7/';url.search='';url.hash=token;
    return url.href;
  }
  root.RemotePairing={parse};
  if(typeof module!=='undefined'&&module.exports)module.exports=root.RemotePairing;
})(typeof window!=='undefined'?window:globalThis);

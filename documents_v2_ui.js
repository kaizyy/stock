(()=>{
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function enhance(){
  document.querySelectorAll('.order-card').forEach(card=>{
    if(card.querySelector('.doc-v2-actions'))return;
    const status=card.querySelector('[data-order-status]');const del=card.querySelector('[data-delete-order]');const id=status?.dataset.orderStatus||del?.dataset.deleteOrder;if(!id)return;
    const type=status?.dataset.orderType||del?.dataset.orderType||'';
    const wrap=document.createElement('div');wrap.className='doc-v2-actions';
    const links=[];
    links.push(`<a class="button ghost" target="_blank" href="/api/documents/packing-slip.pdf?id=${encodeURIComponent(id)}">Pakbon</a>`);
    links.push(`<a class="button ghost" target="_blank" href="/api/documents/return.pdf?id=${encodeURIComponent(id)}">Retour</a>`);
    if(type==='sales')links.unshift(`<a class="button ghost" target="_blank" href="/api/documents/invoice.pdf?id=${encodeURIComponent(id)}">Factuur</a>`);
    wrap.innerHTML=links.join('');card.appendChild(wrap);
  });
}
const style=document.createElement('style');style.textContent='.doc-v2-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.doc-v2-actions .button{padding:7px 9px;text-decoration:none}@media(max-width:600px){.doc-v2-actions{display:grid;grid-template-columns:1fr 1fr}.doc-v2-actions .button{text-align:center}.doc-v2-actions .button:first-child:last-child{grid-column:1/-1}}';document.head.appendChild(style);
const observer=new MutationObserver(()=>enhance());window.addEventListener('load',()=>{enhance();const root=document.getElementById('orders');if(root)observer.observe(root,{childList:true,subtree:true})});document.addEventListener('stockroom:refresh',()=>setTimeout(enhance,200));
})();
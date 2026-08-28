(()=>{
  function enhance(){
    document.querySelectorAll('#financeList .finance-card').forEach(card=>{
      const actions=card.querySelector('.finance-actions');
      if(!actions||actions.querySelector('[data-fin-delete]'))return;
      const source=actions.querySelector('[data-fin-pay],[data-fin-remind],[data-fin-credit]');
      const pdf=actions.querySelector('a[href*="invoice.pdf?id="]');
      const orderId=source?.dataset.finPay||source?.dataset.finRemind||source?.dataset.finCredit||new URL(pdf?.href||location.href).searchParams.get('id');
      if(!orderId)return;
      const number=card.querySelector('.finance-head strong')?.textContent?.trim()||'Deze factuur';
      const button=document.createElement('button');button.type='button';button.className='button ghost finance-delete';button.dataset.finDelete=orderId;button.dataset.invoiceNumber=number;button.textContent='Verwijderen';actions.appendChild(button)
    })
  }
  async function remove(button){
    const number=button.dataset.invoiceNumber||'Deze factuur';
    if(!confirm(`${number} verwijderen? Ook gekoppelde betalingen en creditnota's worden verwijderd. De order blijft behouden.`))return;
    button.disabled=true;
    try{const body=new FormData();body.set('order_id',button.dataset.finDelete);const response=await fetch('/api/finance/delete',{method:'POST',body});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||'Factuur verwijderen mislukt.');button.closest('.finance-card')?.remove();document.dispatchEvent(new CustomEvent('stockroom:refresh'));document.getElementById('refreshFinance')?.click()}
    catch(error){button.disabled=false;alert(error.message)}
  }
  document.addEventListener('click',event=>{const button=event.target.closest('[data-fin-delete]');if(button){event.preventDefault();remove(button)}if(event.target.closest('[data-view="finance"],#refreshFinance'))setTimeout(enhance,350)});
  const observer=new MutationObserver(enhance);window.addEventListener('load',()=>{const list=document.getElementById('financeList');if(list)observer.observe(list,{childList:true,subtree:true});setTimeout(enhance,500)});
})();


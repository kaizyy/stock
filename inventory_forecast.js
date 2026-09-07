(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.StockroomForecast=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  const DAY=86400000;
  function calculate(state,reservations=[],now=Date.now(),options={}){
    const historyDays=Number(options.historyDays||90),horizonDays=Number(options.horizonDays||30),leadDays=Number(options.leadDays||14),reservedByItem={};
    for(const row of reservations)reservedByItem[String(row.item_id)]=Number(row.reserved||0);
    const since=now-historyDays*DAY;
    return (state.items||[]).filter(item=>!item.archived).map(item=>{
      const itemId=String(item.id),sold=(state.transactions||[]).filter(t=>t.type==='outgoing'&&String(t.itemId)===itemId&&new Date(t.date).getTime()>=since&&new Date(t.date).getTime()<=now).reduce((sum,t)=>sum+Number(t.qty||0),0);
      const daily=sold/historyDays,reserved=reservedByItem[itemId]||0,stock=Number(item.stock||0),available=Math.max(0,stock-reserved);
      const incoming=(state.transactions||[]).filter(t=>t.type==='incoming'&&String(t.itemId)===itemId&&!t.done).reduce((sum,t)=>sum+Number(t.qty||0),0);
      const minimum=Math.max(0,Number(item.minStock||0)),target=Math.max(minimum,Math.ceil(daily*(horizonDays+leadDays)+minimum));
      const recommended=Math.max(0,Math.ceil(target-available-incoming)),daysCover=daily>0?available/daily:null;
      const urgency=available<=0?'critical':recommended>0&&(daysCover===null||daysCover<=leadDays)?'order':recommended>0?'plan':'healthy';
      return {itemId,name:item.name||item.sku||'Artikel',sku:item.sku||'',stock,reserved,available,incoming,sold,daily,daysCover,recommended,urgency};
    }).sort((a,b)=>({critical:0,order:1,plan:2,healthy:3}[a.urgency]-{critical:0,order:1,plan:2,healthy:3}[b.urgency])||b.recommended-a.recommended||a.name.localeCompare(b.name));
  }
  return {calculate};
});

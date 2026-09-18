"""Evidence-based supplier performance and purchase-price history."""
from collections import defaultdict

import server


def overview(stockroom_id):
    with server.db() as conn:
        rows=conn.execute("""SELECT o.id::text order_id,o.relation_id::text supplier_id,
                   COALESCE(s.name,o.relation_name,'Geen leverancier') supplier_name,o.order_date,
                   l.item_id,l.item_name,l.sku,l.quantity::float8,l.fulfilled_quantity::float8,l.unit_price::float8,
                   (SELECT MIN(pr.received_at) FROM purchase_receipts pr WHERE pr.order_id=o.id AND pr.reversed_at IS NULL) first_received_at,
                   COALESCE((SELECT SUM(rl.quantity) FROM order_return_lines rl JOIN order_returns r ON r.id=rl.return_id
                     WHERE rl.order_line_id=l.id AND r.status<>'cancelled'),0)::float8 returned_quantity
            FROM orders o JOIN order_lines l ON l.order_id=o.id LEFT JOIN suppliers s ON s.id=o.relation_id
            WHERE o.stockroom_id=%s AND o.order_type='purchase' AND o.status<>'cancelled'
            ORDER BY o.order_date,o.created_at,l.created_at""",(stockroom_id,)).fetchall()
    suppliers={};item_suppliers=defaultdict(lambda:defaultdict(list))
    for row in rows:
        key=row['supplier_id'] or f"name:{row['supplier_name']}"
        supplier=suppliers.setdefault(key,{'id':row['supplier_id'],'name':row['supplier_name'],'orders':set(),'ordered':0.0,'received':0.0,'returned':0.0,'lead_days':{}})
        supplier['orders'].add(row['order_id']);supplier['ordered']+=float(row['quantity']);supplier['received']+=float(row['fulfilled_quantity']);supplier['returned']+=float(row['returned_quantity'])
        if row['first_received_at']:supplier['lead_days'].setdefault(row['order_id'],max(0,(row['first_received_at'].date()-row['order_date']).days))
        item_suppliers[str(row['item_id'])][key].append(row)
    supplier_rows=[]
    for key,supplier in suppliers.items():
        lead_values=list(supplier['lead_days'].values());avg_lead=sum(lead_values)/len(lead_values) if lead_values else None;return_rate=supplier['returned']/supplier['received']*100 if supplier['received'] else 0;history=len(supplier['orders'])
        delivery_score=max(0,100-(avg_lead or 21)*2);quality_score=max(0,100-return_rate*2);confidence=min(100,history*20);score=round(quality_score*.5+delivery_score*.3+confidence*.2)
        supplier_rows.append({'key':key,'id':supplier['id'],'name':supplier['name'],'orders':history,'avgLeadDays':round(avg_lead,1) if avg_lead is not None else None,'returnRate':round(return_rate,1),'score':score,'confidence':'high' if history>=5 else 'medium' if history>=2 else 'low'})
    score_by_key={row['key']:row['score'] for row in supplier_rows};recommendations=[];price_history=[]
    for item_id,choices in item_suppliers.items():
        latest_prices={key:float(entries[-1]['unit_price']) for key,entries in choices.items()};minimum=min(latest_prices.values()) if latest_prices else 0;ranked=[]
        for key,entries in choices.items():
            latest=float(entries[-1]['unit_price']);previous=float(entries[-2]['unit_price']) if len(entries)>1 else None;change=((latest-previous)/previous*100) if previous else None;price_score=100 if latest<=0 or minimum<=0 else min(100,minimum/latest*100);combined=round(score_by_key.get(key,0)*.7+price_score*.3)
            ranked.append({'supplierKey':key,'supplierId':entries[-1]['supplier_id'],'supplierName':entries[-1]['supplier_name'],'latestPrice':latest,'previousPrice':previous,'priceChange':round(change,1) if change is not None else None,'score':combined,'samples':len(entries)})
            for entry in entries:price_history.append({'itemId':item_id,'itemName':entry['item_name'],'supplierName':entry['supplier_name'],'date':entry['order_date'],'price':float(entry['unit_price'])})
        ranked.sort(key=lambda row:(-row['score'],row['latestPrice'],row['supplierName']));source=next(iter(choices.values()))[-1]
        recommendations.append({'itemId':item_id,'itemName':source['item_name'],'sku':source['sku'],'recommended':ranked[0] if ranked else None,'alternatives':ranked})
    supplier_rows.sort(key=lambda row:(-row['score'],row['name']));recommendations.sort(key=lambda row:row['itemName'].lower());price_history.sort(key=lambda row:(row['itemName'].lower(),row['date']),reverse=True)
    return {'suppliers':supplier_rows,'recommendations':recommendations,'priceHistory':price_history[:250]}

"""Monthly budgets, actuals and end-of-month variance forecasts."""
import json
from calendar import monthrange
from datetime import date

import profit_reporting
import server


def initialize():
    with server.db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS monthly_budgets(
            stockroom_id UUID NOT NULL REFERENCES stockrooms(id) ON DELETE CASCADE,
            budget_year INTEGER NOT NULL CHECK(budget_year BETWEEN 2000 AND 2100),
            budget_month INTEGER NOT NULL CHECK(budget_month BETWEEN 1 AND 12),
            revenue_target NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(revenue_target>=0),
            gross_profit_target NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(gross_profit_target>=0),
            expense_limit NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(expense_limit>=0),
            updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(stockroom_id,budget_year,budget_month))""")
        conn.commit()


def _number(value, label):
    try: result=round(float(value or 0),2)
    except (TypeError,ValueError): raise ValueError(f'Vul een geldig budget voor {label} in.')
    if result<0: raise ValueError(f'Het budget voor {label} kan niet negatief zijn.')
    return result


def _period(year, month):
    try: year=int(year);month=int(month)
    except (TypeError,ValueError): raise ValueError('Kies een geldige maand.')
    if not 2000<=year<=2100 or not 1<=month<=12: raise ValueError('Kies een geldige maand.')
    return year,month


def save(session, values):
    if session.get('role') not in ('owner','admin'): raise PermissionError('Alleen eigenaar of beheerder kan budgetten wijzigen.')
    year,month=_period(values.get('year'),values.get('month'))
    revenue=_number(values.get('revenue_target'),'omzet');gross=_number(values.get('gross_profit_target'),'brutomarge');expenses=_number(values.get('expense_limit'),'bedrijfskosten')
    with server.db() as conn:
        conn.execute("""INSERT INTO monthly_budgets(stockroom_id,budget_year,budget_month,revenue_target,gross_profit_target,expense_limit,updated_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(stockroom_id,budget_year,budget_month) DO UPDATE SET
            revenue_target=EXCLUDED.revenue_target,gross_profit_target=EXCLUDED.gross_profit_target,expense_limit=EXCLUDED.expense_limit,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
            (session['stockroom_id'],year,month,revenue,gross,expenses,session['user_id']))
        conn.execute("INSERT INTO audit_log(stockroom_id,user_id,action,details) VALUES(%s,%s,'budget.updated',%s::jsonb)",
            (session['stockroom_id'],session['user_id'],json.dumps({'year':year,'month':month,'revenue':revenue,'grossProfit':gross,'expenses':expenses})))
        conn.commit()
    return {'saved':True}


def _metric(actual, target, projected, higher_is_better=True):
    variance=round(actual-target,2);forecast_variance=round(projected-target,2)
    favorable=variance>=0 if higher_is_better else variance<=0
    forecast_favorable=forecast_variance>=0 if higher_is_better else forecast_variance<=0
    return {'actual':actual,'target':target,'variance':variance,'favorable':favorable,'forecast':projected,'forecastVariance':forecast_variance,'forecastFavorable':forecast_favorable,'progressPercent':round(actual/target*100,1) if target else None}


def overview(stockroom_id, year, month):
    year,month=_period(year,month);today=date.today();days=monthrange(year,month)[1]
    if (year,month)==(today.year,today.month): elapsed=max(1,today.day);forecast_factor=days/elapsed
    elif (year,month)<(today.year,today.month): elapsed=days;forecast_factor=1
    else: elapsed=0;forecast_factor=0
    actual=profit_reporting.report(stockroom_id,year,'month',month)['summary']
    with server.db() as conn:
        row=conn.execute("SELECT revenue_target::float8,gross_profit_target::float8,expense_limit::float8 FROM monthly_budgets WHERE stockroom_id=%s AND budget_year=%s AND budget_month=%s",(stockroom_id,year,month)).fetchone()
    budget={'revenue':float((row or {}).get('revenue_target') or 0),'grossProfit':float((row or {}).get('gross_profit_target') or 0),'operatingExpenses':float((row or {}).get('expense_limit') or 0)}
    projected={key:round(float(actual[key])*forecast_factor,2) for key in budget}
    metrics={
        'revenue':_metric(float(actual['revenue']),budget['revenue'],projected['revenue']),
        'grossProfit':_metric(float(actual['grossProfit']),budget['grossProfit'],projected['grossProfit']),
        'operatingExpenses':_metric(float(actual['operatingExpenses']),budget['operatingExpenses'],projected['operatingExpenses'],False)
    }
    alerts=[];pace=elapsed/days if days else 1
    labels={'revenue':'Omzet','grossProfit':'Brutomarge','operatingExpenses':'Bedrijfskosten'}
    for key,metric in metrics.items():
        target=metric['target']
        if not target: continue
        expected=target*pace
        behind=metric['actual']<expected*.9 if key!='operatingExpenses' else metric['actual']>expected*1.1
        if behind: alerts.append({'severity':'warning','metric':key,'message':f"{labels[key]} wijkt ongunstig af van het maandtempo."})
        if not metric['forecastFavorable']: alerts.append({'severity':'danger','metric':key,'message':f"Verwachting einde maand: {labels[key].lower()} mist het budget met € {abs(metric['forecastVariance']):,.2f}."})
    if not any(budget.values()): alerts.append({'severity':'warning','metric':'budget','message':'Voor deze maand zijn nog geen budgetten ingesteld.'})
    return {'period':{'year':year,'month':month,'daysElapsed':elapsed,'daysTotal':days},'budget':budget,'actual':actual,'metrics':metrics,'alerts':alerts,
        'links':{'revenue':{'view':'finance','label':'Bekijk verkoopfacturen'},'grossProfit':{'view':'outgoing','label':'Bekijk verkooptransacties'},'operatingExpenses':{'view':'analytics','anchor':'expenseManager','label':'Bekijk kostenposten'}}}

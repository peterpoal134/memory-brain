"""Versioned official reference estimates, never provider invoices or guessed tokens."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP

VERSION='deepseek-flash-cn-20260910'
VERIFIED_AT='2026-11-09'
SOURCE='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
CALENDAR_SOURCE='https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm'
BJT=timezone(timedelta(hours=8))
EFFECTIVE=datetime(2026,9,10,12,tzinfo=BJT).timestamp()
MODELS={'deepseek-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp','deepseek-v4.1-flash-1017'}
RATES={'off_peak':{'cache_hit':'0.02','cache_miss':'1','output':'4'},
       'peak':{'cache_hit':'0.04','cache_miss':'2','output':'8'}}
# Official rest spans include adjusted days. The supplier has not published a
# day-by-day billing calendar: an ambiguous weekday within these spans gets a
# range, rather than treating every adjusted day as a proven statutory holiday.
HOLIDAY_SPANS_2026=[('01-01','01-03'),('02-15','02-23'),('04-04','04-06'),
    ('05-01','05-05'),('06-19','06-21'),('09-25','09-27'),('10-01','10-07')]
MILLION=Decimal(1_000_000)
QUANTUM=Decimal('0.00000001')


def amount(value):
    return format(value.quantize(QUANTUM,rounding=ROUND_HALF_UP),'f')


def price_info():
    return {'version':VERSION,'currency':'CNY','unit':'每百万 tokens','model':'DeepSeek Flash',
        'rates':RATES,'source_url':SOURCE,'calendar_source_url':CALENDAR_SOURCE,'verified_at':VERIFIED_AT,
        'effective_from':datetime.fromtimestamp(EFFECTIVE,BJT).isoformat(),'timezone':'Asia/Shanghai',
        'peak_hours':'北京时间周一至周五09:00–12:00、14:00–18:00，中国法定节假日除外',
        'holiday_note':'周末始终闲时；供应商未公开逐日节假日计费表，调休假期内的平日及未核实年份按区间。',
        'billing_note':'人民币官方参考估算，不是账单；未核实路由是否直连。缺用量不当免费，不含搜索/抓取及其它接口费用、代理加价或账号折扣。'}


def _periods_at(stamp):
    at=datetime.fromtimestamp(stamp,BJT)
    minute=at.hour*60+at.minute
    if at.weekday()>=5 or not (540<=minute<720 or 840<=minute<1080):
        return {'off_peak'},None
    if at.year!=2026:
        return {'off_peak','peak'},'节假日日历年份未核实'
    md=at.strftime('%m-%d')
    if any(start<=md<=end for start,end in HOLIDAY_SPANS_2026):
        return {'off_peak','peak'},'假期含调休，供应商逐日计费日历未公开'
    return {'peak'},None


def estimate(row):
    """One request; output tokens already include reasoning when host reports it."""
    result={'estimated_cost_cny':None,'cost_low_cny':None,'cost_high_cny':None,
        'price_period':'uncertain','price_note':'','time_basis':'unavailable','pricing_version':row.get('pricing_version',VERSION)}
    if row.get('model','').casefold() not in MODELS:
        result['price_note']='模型未匹配官方Flash价格，未计价';return result
    if row.get('pricing_version',VERSION)!=VERSION:
        result['price_note']='历史价格版本不可用，未计价';return result
    end=row.get('ended_at');start=row.get('started_at')
    if end is None and start is None:
        result['price_note']='请求时间未报告，未计价';return result
    time_basis='started_and_ended' if start is not None and end is not None else 'ended_at_assumption' if end is not None else 'started_at_assumption'
    end=end if end is not None else start
    start=start if start is not None else end
    if min(start,end)<EFFECTIVE:
        result['price_note']='早于已核实价格生效时间，未计价';return result
    values=[row.get(key) for key in ('cache_hit_tokens','cache_miss_tokens','output_tokens')]
    if any(value is None for value in values):
        result['price_note']='缓存拆分或输出用量未报告，未计价';return result
    instants=[start,end];notes=[]
    if end-start>48*3600:
        periods={'off_peak','peak'};notes.append('时间跨度过大，按两档范围')
    else:
        periods=set()
        day=datetime.fromtimestamp(start,BJT).replace(hour=0,minute=0,second=0,microsecond=0)
        while day.timestamp()<=end:
            for hour in (0,9,12,14,18):
                bound=(day+timedelta(hours=hour)).timestamp()
                if start<=bound<=end:
                    instants.append(bound)
                    if start<bound:instants.append(bound-.001)
            day+=timedelta(days=1)
        for stamp in instants:
            choices,note=_periods_at(stamp);periods.update(choices)
            if note and note not in notes:notes.append(note)
    prices=[]
    for period in periods:
        rate=RATES[period]
        prices.append(sum(Decimal(value)*Decimal(rate[key]) for value,key in zip(values,('cache_hit','cache_miss','output')))/MILLION)
    low,high=min(prices),max(prices)
    result.update(cost_low_cny=amount(low),cost_high_cny=amount(high),time_basis=time_basis,
        price_period=next(iter(periods)) if len(periods)==1 else 'uncertain')
    if len(periods)==1:result['estimated_cost_cny']=amount(low)
    else:notes.append('跨档或日历不确定，按价格范围估算')
    if time_basis=='ended_at_assumption':notes.append('开始时间缺失，按结束时刻参考估算')
    if time_basis=='started_at_assumption':notes.append('结束时间缺失，按开始时刻参考估算')
    if row.get('provider')!='deepseek_official':notes.append('提供商未核实，仅官方参考价')
    notes.append('估算非账单')
    result['price_note']='；'.join(notes)
    return result

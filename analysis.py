"""Deterministic timing analysis. Never equates matched-lap time with race gap."""
import io, math, statistics
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

CAVEAT = ('These are observed lap-time differences, not proof of why they happened. '
          'Tyres, traffic, weather and strategy can confound the comparison. '
          'Summed matched-lap deltas are not the on-track race gap.')

def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def analyze(data, drivers, start, end):
    a, b = drivers
    roster = {x['driver_number']:x for x in data['drivers']}
    if a == b or a not in roster or b not in roster:
        raise ValueError('Choose two different drivers present in this session.')
    if not 1 <= start <= end <= 100:
        raise ValueError('Lap range must be ordered and between 1 and 100.')
    pits = {d:set() for d in drivers}
    for x in data.get('pit', []):
        if x.get('driver_number') in pits and x.get('lap_number'):
            pits[x['driver_number']].update([x['lap_number'], x['lap_number']+1])
    maps = {d:{} for d in drivers}
    duplicate = set()
    for x in data['laps']:
        d, n = x.get('driver_number'), x.get('lap_number')
        if d in maps and n and start <= n <= end:
            if n in maps[d]: duplicate.add((d,n))
            maps[d][n] = x
    caution = []
    for x in data.get('race_control', []):
        if x.get('date') and (x.get('flag') in ['YELLOW','DOUBLE YELLOW','RED'] or x.get('category') == 'SafetyCar'):
            caution.append(timestamp(x['date']))
    rows, excluded = [], []
    for n in range(start,end+1):
        reason = None
        pair = [maps[d].get(n) for d in drivers]
        if n == 1: reason = 'Standing-start lap'
        elif any((d,n) in duplicate for d in drivers): reason = 'Duplicate source lap'
        elif any(x is None for x in pair): reason = 'Missing matched lap'
        elif any(n in pits[d] or maps[d][n].get('is_pit_out_lap') for d in drivers): reason = 'Pit entry / out-lap'
        elif any(not isinstance(x.get('lap_duration'), (int,float)) or isinstance(x.get('lap_duration'),bool) or not math.isfinite(x['lap_duration']) or x['lap_duration'] <= 0 for x in pair): reason = 'Invalid duration'
        else:
            for x in pair:
                if x.get('date_start'):
                    t = timestamp(x['date_start'])
                    if any(t <= c <= t+timedelta(seconds=x['lap_duration']) for c in caution):
                        reason = 'Race-control caution event overlaps lap'
        if reason:
            excluded.append({'lap':n, 'reason':reason})
        else:
            rows.append({'lap':n, 'a':pair[0]['lap_duration'], 'b':pair[1]['lap_duration'], 'delta':round(pair[0]['lap_duration']-pair[1]['lap_duration'],3)})
    if len(rows)<3:
        raise ValueError('Insufficient evidence: fewer than three valid matched laps. Widen the range.')
    names = [roster[d].get('full_name', str(d)).title() for d in drivers]
    delta = round(statistics.mean(r['delta'] for r in rows),3)
    faster = names[0] if delta < 0 else names[1] if delta > 0 else 'Neither driver'
    facts = [
      {'id':'F1','text':f"The comparison covers laps {start}–{end}, using {len(rows)} valid matched laps and excluding {len(excluded)} {'lap' if len(excluded)==1 else 'laps'}.", 'source':'laps'},
      {'id':'F2','text':f'{names[0]} averaged {statistics.mean(r["a"] for r in rows):.3f} seconds per included lap; {names[1]} averaged {statistics.mean(r["b"] for r in rows):.3f} seconds.', 'source':'laps'},
      {'id':'F3','text':f'{faster} was quicker by {abs(delta):.3f} seconds per lap on average in this matched sample.' if delta else 'The drivers had the same mean lap time in this matched sample.', 'source':'laps'},
      {'id':'F4','text':f'The sum of included lap-time differences (first driver minus second) is {sum(r["delta"] for r in rows):+.3f} seconds. This is not the race gap.', 'source':'laps'},
    ]
    for d,name in zip(drivers,names):
        for p in data.get('pit',[]):
            if p.get('driver_number') == d and start <= (p.get('lap_number') or 0) <= end:
                duration = p.get('lane_duration', p.get('pit_duration'))
                if isinstance(duration,(int,float)):
                    facts.append({'id':f'F{len(facts)+1}','text':f'{name} recorded {duration:.3f} seconds of pit-lane time on lap {p["lap_number"]}. Pit-lane time is not stationary stop time.', 'source':'pit'})
    stints = [s for s in data.get('stints',[]) if s.get('driver_number') in drivers and (s.get('lap_end') or 100)>=start and (s.get('lap_start') or 0)<=end]
    return {'drivers':drivers,'names':names,'start':start,'end':end,'rows':rows,'excluded':excluded,'facts':facts,'mean_delta':delta,'faster':faster,'caveat':CAVEAT,'stints':stints,'method':'Matched lap numbers; exclude lap 1, missing/invalid/duplicate laps, pit entry and next lap, flagged out-laps, and laps overlapping a caution message. Caution intervals are not fully reconstructed; this is not a clean-air pace estimate.'}

def chart(result, label):
    with plt.rc_context({'font.family':'DejaVu Sans','text.color':'#e9ebef','axes.labelcolor':'#aab2c3','xtick.color':'#aab2c3','ytick.color':'#aab2c3','axes.edgecolor':'#30394b'}):
        fig,(ax,bx)=plt.subplots(2,1,figsize=(12,8),gridspec_kw={'height_ratios':[2,1]},facecolor='#10151e')
        for a in (ax,bx):
            a.set_facecolor('#10151e'); a.grid(alpha=.12); a.spines[['top','right']].set_visible(False)
        rows=result['rows']; xs=[r['lap'] for r in rows]
        for key,color,name in zip(['a','b'],['#ff6738','#59d7cc'],result['names']):
            # NaNs preserve excluded-lap gaps instead of connecting them.
            vals={r['lap']:r[key] for r in rows}
            full=range(result['start'],result['end']+1)
            ax.plot(list(full),[vals.get(n,float('nan')) for n in full],color=color,lw=2.2,marker='.',label=name)
        ax.set_ylabel('Lap duration (seconds)'); ax.legend(facecolor='#10151e',edgecolor='none',labelcolor='white')
        bx.bar(xs,[r['delta'] for r in rows],color=['#59d7cc' if r['delta']>0 else '#ff6738' for r in rows])
        bx.axhline(0,color='white',lw=.5); bx.set_ylabel('A − B (seconds)'); bx.set_xlabel('Lap number')
        fig.suptitle('PITWALL  /  '+label,fontsize=19,fontweight='bold',x=.08,ha='left')
        fig.text(.08,.025,'Source: OpenF1 • matched laps only • negative delta favors first driver • not race gap',fontsize=10,color='#aab2c3')
        fig.tight_layout(rect=[.02,.055,.98,.95]); buf=io.BytesIO(); fig.savefig(buf,format='png',dpi=140,facecolor=fig.get_facecolor()); plt.close(fig)
        return buf.getvalue()

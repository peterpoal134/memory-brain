"""Calendar references from actual quoted spans; no model or wall clock."""
import copy
import datetime as dt
import json
import unittest

from xiaojiu_brain.temporal import normalize_time,source_times,time_metadata


def message(text,iso='2026-06-10T00:30:00+08:00',ident=1):
    return {'id':ident,'text':text,'recorded_at':dt.datetime.fromisoformat(iso).timestamp(),
            'metadata':{'recorded_at_iso':iso}}


class TemporalTests(unittest.TestCase):
    def normalize(self,expression,iso='2026-06-10T00:30:00+08:00',timezone='Asia/Shanghai'):
        return normalize_time(expression,message('已引用：'+expression,iso),timezone)

    def test_strict_iso_chinese_and_explicit_english_absolute_days(self):
        for expression in ('2031-11-26','2031年11月26日','November 26, 2031','26 Nov 2031'):
            with self.subTest(expression=expression):
                result=self.normalize(expression)
                self.assertEqual(result['status'],'resolved')
                self.assertEqual(result['start'],'2031-11-26T00:00:00+08:00')
                self.assertEqual(result['end'],'2031-11-27T00:00:00+08:00')
                self.assertEqual(result['precision'],'day')

    def test_relative_days_preserve_anchor_offset_and_use_configured_local_calendar(self):
        for expression,day in [('今天','10'),('昨天','09'),('前天','08'),('明天','11'),('后天','12'),
                               ('today','10'),('the day before yesterday','08'),('the day after tomorrow','12')]:
            result=self.normalize(expression)
            self.assertEqual(result['start'],'2026-06-'+day+'T00:00:00+08:00')
            self.assertEqual(result['anchor_recorded_at_iso'],'2026-06-10T00:30:00+08:00')
        utc=self.normalize('今天',timezone='UTC')
        self.assertEqual(utc['start'],'2026-06-09T00:00:00+00:00')
        self.assertEqual(utc['anchor_recorded_at_iso'],'2026-06-10T00:30:00+08:00')

    def test_calendar_weekday_anchors_are_monday_based(self):
        for expression,day in [('上周五','05'),('上周星期五','05'),('本周五','12'),('下星期一','15'),
                               ('last week Friday','05'),('Friday of last week','05'),('this Friday','12'),
                               ('next week Monday','15')]:
            result=self.normalize(expression)
            self.assertEqual(result['status'],'resolved',result)
            self.assertEqual(result['start'],'2026-06-'+day+'T00:00:00+08:00')
        week=self.normalize('下周')
        self.assertEqual(week['start'],'2026-06-15T00:00:00+08:00')
        self.assertEqual(week['end'],'2026-06-22T00:00:00+08:00')
        self.assertEqual(week['precision'],'week')

    def test_missing_year_vague_and_convention_ambiguous_dates_are_not_guessed(self):
        for expression in ('6月5日','June 5','5 June','6/5','上旬','下月下旬','改天',
                           'last Friday','next Monday','星期五','Friday'):
            with self.subTest(expression=expression):
                result=self.normalize(expression)
                self.assertEqual(result['status'],'ambiguous',result)
                self.assertIsNone(result['start']);self.assertIsNone(result['end'])

    def test_day_and_week_offsets_use_calendar_arithmetic(self):
        for expression,day in [('3天前','07'),('三天后','13'),('两周后','24'),('一周前','03'),
                               ('3 days ago','07'),('in 3 days','13'),('2 weeks from now','24')]:
            result=self.normalize(expression)
            self.assertEqual(result['start'],'2026-06-'+day+'T00:00:00+08:00')
        self.assertEqual(self.normalize('十五天后')['start'],'2026-06-25T00:00:00+08:00')

    def test_unanchored_before_after_and_later_never_use_the_message_day(self):
        original=message('The review is 2 days before the launch on June 20 2026.')
        items=source_times(original,0,len(original['text']),'Asia/Shanghai')
        self.assertEqual([item['expression'] for item in items],['2 days before','June 20 2026'])
        self.assertEqual(items[0]['status'],'ambiguous')
        self.assertEqual(items[0]['reason'],'external_event_anchor_required')
        self.assertIsNone(items[0]['start'])
        self.assertEqual(items[1]['start'],'2026-06-20T00:00:00+08:00')
        for expression in ('2 days before','3 weeks after','2 days later','会后三天','上线前2天','演出后两周'):
            result=self.normalize(expression)
            self.assertEqual(result['status'],'ambiguous',result)
            self.assertEqual(result['reason'],'external_event_anchor_required')
            self.assertIsNone(result['start']);self.assertIsNone(result['end'])
        for expression in ('2 days ago','2 days from now','in 2 days'):
            self.assertEqual(self.normalize(expression)['status'],'resolved')

    def test_chinese_external_event_scan_retains_unresolved_reference(self):
        original=message('会后三天准备复核；上线前2天停止收件；明天再确认。')
        items=source_times(original,0,len(original['text']),'Asia/Shanghai')
        self.assertEqual([item['expression'] for item in items],['会后三天','上线前2天','明天'])
        self.assertEqual([item['status'] for item in items],['ambiguous','ambiguous','resolved'])

    def test_dst_days_are_23_and_25_hours_instead_of_fixed_seconds(self):
        for iso,hours in [('2026-03-08T12:00:00-04:00',23),('2026-11-01T12:00:00-05:00',25)]:
            result=self.normalize('今天',iso,timezone='America/New_York')
            self.assertEqual(result['status'],'resolved')
            begin=dt.datetime.fromisoformat(result['start']);end=dt.datetime.fromisoformat(result['end'])
            self.assertEqual((end.timestamp()-begin.timestamp())/3600,hours)

    def test_exact_literal_and_cited_unicode_span_are_required(self):
        original=message('🪐未引用昨天；公开引用明天；私密后天。')
        start=original['text'].index('公开');end=original['text'].index('；私密')
        result=normalize_time('明天',original,'Asia/Shanghai',source_start=start,source_end=end)
        self.assertEqual(result['status'],'resolved');self.assertEqual(result['source_ref']['start'],start)
        for expression in ('昨天','后天','明日'):
            self.assertEqual(normalize_time(expression,original,'Asia/Shanghai',source_start=start,source_end=end)['status'],'unsupported')
        for a,b in [(True,end),(start,9999),(end,start),(-1,end)]:
            self.assertEqual(normalize_time('明天',original,'Asia/Shanghai',source_start=a,source_end=b)['reason'],'invalid_source_span')

    def test_model_normalized_start_and_status_are_ignored_and_plan_is_not_occurrence(self):
        original=message('我计划明天再决定是否参加，尚未出发。')
        metadata={'event_time':{'expression':'明天','anchor_message_id':1,'start':'1900-01-01',
                    'end':'9999-01-01','status':'occurred','completed':True},'assertion_type':'plan'}
        result=time_metadata(metadata,[(original,0,len(original['text']))],'Asia/Shanghai')
        self.assertEqual(result['start'],'2026-06-11T00:00:00+08:00')
        self.assertEqual(result['semantics'],'date_reference_only_not_occurrence')
        self.assertNotIn('completed',result);self.assertEqual(metadata['assertion_type'],'plan')
        self.assertEqual(metadata['event_time']['start'],'1900-01-01')

    def test_metadata_string_requires_unique_source_and_explicit_anchor_must_be_cited(self):
        first=message('明天准备交稿',ident=1);second=message('明天计划改期',iso='2027-02-01T09:00:00+08:00',ident=2)
        checked=[(first,0,len(first['text'])),(second,0,len(second['text']))]
        result=time_metadata({'event_time':'明天'},checked,'Asia/Shanghai')
        self.assertEqual(result['status'],'ambiguous');self.assertEqual(result['reason'],'multiple_possible_anchors')
        selected=time_metadata({'event_time':{'expression':'明天','anchor_message_id':2}},checked,'Asia/Shanghai')
        self.assertEqual(selected['start'],'2027-02-02T00:00:00+08:00')
        missing=time_metadata({'event_time':{'expression':'明天','anchor_message_id':3}},checked,'Asia/Shanghai')
        self.assertEqual(missing['status'],'unsupported')
        self.assertEqual(time_metadata({'event_time':'明天'},[],'Asia/Shanghai')['status'],'unsupported')
        self.assertIsNone(time_metadata({},checked))

    def test_flat_and_nested_times_revalidate_and_do_not_choose_conflicting_proposals(self):
        original=message('明天与后天的不同计划。');checked=[(original,0,len(original['text']))]
        both={'event_time':'明天','claim':{'event_time':{'expression':'明天','anchor_message_id':1,
                'start':'1900-01-01','status':'resolved'}}}
        result=time_metadata(both,checked,'Asia/Shanghai')
        self.assertEqual(result['start'],'2026-06-11T00:00:00+08:00')
        both['claim']['event_time']['expression']='后天'
        self.assertEqual(time_metadata(both,checked)['reason'],'conflicting_time_proposals')
        both['event_time']={'expression':'明天','anchor_message_id':1}
        both['claim']['event_time']={'expression':'明天','anchor_message_id':2}
        self.assertEqual(time_metadata(both,checked)['reason'],'conflicting_time_proposals')

    def test_sqlite_json_metadata_keeps_valid_iso_and_distrusts_mismatched_iso(self):
        original=message('today','2026-06-10T01:30:00+09:00')
        original['metadata']=json.dumps(original['metadata'])
        result=normalize_time('today',original,'Asia/Shanghai')
        self.assertEqual(result['anchor_recorded_at_iso'],'2026-06-10T01:30:00+09:00')
        original['metadata']=json.dumps({'recorded_at_iso':'1900-01-01T00:00:00+09:00'})
        result=normalize_time('today',original,'Asia/Shanghai')
        self.assertEqual(result['anchor_recorded_at_iso'],'2026-06-10T00:30:00+08:00')

    def test_imported_note_record_timestamp_never_authorizes_a_utterance_date(self):
        original=message('旧摘要声称明天再确认；旧记录还有2031-11-26。')
        original['metadata']['source_kind']='imported_memory'
        for metadata in (dict(original['metadata']),json.dumps(original['metadata'])):
            original['metadata']=metadata
            result=normalize_time('明天',original,'Asia/Shanghai')
            self.assertEqual(result['status'],'unsupported')
            self.assertEqual(result['reason'],'unverified_source_time')
            self.assertIsNone(result['start']);self.assertIsNone(result['end'])
            self.assertEqual(result['anchor_recorded_at_iso'],'2026-06-10T00:30:00+08:00')
            scanned=source_times(original,0,len(original['text']),'Asia/Shanghai')
            self.assertTrue(scanned)
            self.assertTrue(all(item['reason']=='unverified_source_time' and item['start'] is None for item in scanned))

    def test_revalidating_worker_metadata_cannot_promote_imported_record_time(self):
        original=message('历史导入笔记：今天已决定明天参加。')
        original['metadata']['source_kind']='imported_memory'
        checked=[(original,0,len(original['text']))]
        proposal={'event_time':{'expression':'明天','anchor_message_id':1,'status':'resolved',
                               'start':'2026-06-11T00:00:00+08:00'}}
        first=time_metadata(proposal,checked,'Asia/Shanghai')
        self.assertEqual(first['reason'],'unverified_source_time')
        proposal['claim']={'event_time':dict(first,status='resolved',start='2026-06-11T00:00:00+08:00')}
        again=time_metadata(proposal,checked,'Asia/Shanghai')
        self.assertEqual(again['reason'],'unverified_source_time')
        self.assertIsNone(again['start']);self.assertIsNone(again['end'])

    def test_invalid_or_unknown_values_return_unresolved_without_throwing_batch(self):
        for expression in ('2026-02-30','2026-2-3','a long time ago','一二天后','9999999999999天后'):
            result=self.normalize(expression)
            self.assertEqual(result['status'],'unsupported',result)
        self.assertEqual(normalize_time('今天',message('今天'),'Not/A_Zone')['reason'],'invalid_timezone')
        bad=message('今天');bad['recorded_at']=float('nan')
        self.assertEqual(normalize_time('今天',bad)['reason'],'invalid_anchor_time')
        self.assertEqual(time_metadata({'event_time':False},[])['reason'],'invalid_time_proposal')

    def test_source_scan_is_ordered_limited_and_never_reads_adjacent_uncited_content(self):
        original=message('私密2029-05-01；公开：明天、下周五、June 5、后天。未引昨天。')
        start=original['text'].index('公开');end=original['text'].index('未引')
        before=copy.deepcopy(original);items=source_times(original,start,end,'Asia/Shanghai')
        self.assertEqual([item['expression'] for item in items],['明天','下周五','June 5'])
        self.assertEqual(items[-1]['status'],'ambiguous')
        for item in items:
            self.assertGreaterEqual(item['expression_start'],start);self.assertLessEqual(item['expression_end'],end)
            self.assertEqual(original['text'][item['expression_start']:item['expression_end']],item['expression'])
        self.assertEqual(original,before)
        self.assertEqual(source_times(original,start,end,'Asia/Shanghai',limit=0),[])

    def test_source_scan_does_not_match_english_word_pieces_or_partial_numeric_dates(self):
        original=message('todayland next Fridayish 1232026-06-05 2026-06-051 2026-06-05T12:00:00; 明天')
        items=source_times(original,0,len(original['text']),'UTC',limit=20)
        self.assertEqual([item['expression'] for item in items],['明天'])


if __name__=='__main__':unittest.main()

import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing
import import_claude


class BillingTests(unittest.TestCase):
    def cloud_snapshot(self, provider='aws', end='2026-10-10'):
        return dict(schema_version=1, provider=provider, start='2026-09-01', end=end,
                    months=['2026-09', '2026-10'], extracted_at='old',
                    meta=({'metric':'UnblendedCost', 'source':'AWS Cost Explorer', 'profile':'billing',
                           'estimated_months':['2026-09','2026-10']} if provider == 'aws' else
                          {'metric':'cost + credits', 'source':'project.dataset.table', 'estimated_months':[]}),
                    records=[dict(provider=provider, month=m, entity_id='a', entity_name='Account',
                                  service='Compute', currency='USD', amount=10) for m in ['2026-09','2026-10']])

    def test_append_replaces_month_and_preserves_history_and_provenance(self):
        old=self.cloud_snapshot()
        fresh={**old, 'start':'2026-10-01', 'end':'2026-10-15', 'months':['2026-10'],
               'extracted_at':'new', 'meta':{**old['meta'], 'estimated_months':[]},
               'records':[{**old['records'][1], 'amount':25}]}
        merged=billing.merge_snapshot(old,fresh)
        self.assertEqual(merged['records'],[old['records'][0],fresh['records'][0]])
        self.assertEqual(merged['meta']['estimated_months'],['2026-09'])
        self.assertEqual(merged['meta']['month_extracted_at'],{'2026-09':'old','2026-10':'new'})
        self.assertEqual(billing.merge_snapshot(merged,fresh),merged)
        self.assertEqual(billing.merge_snapshot(old,{**fresh,'records':[]})['records'],[old['records'][0]])

    def test_append_accepts_month_boundary_but_rejects_gaps_cutoff_and_metric_changes(self):
        old=self.cloud_snapshot(end='2026-11-01')
        fresh={**old, 'start':'2026-11-01','end':'2026-11-15','months':['2026-11'],'records':[]}
        self.assertEqual(billing.merge_snapshot(old,fresh)['months'],['2026-09','2026-10','2026-11'])
        for invalid in [{**fresh,'start':'2026-12-01'}, {**fresh,'end':'2026-10-15'},
                        {**fresh,'meta':{**fresh['meta'],'metric':'AmortizedCost'}}]:
            with self.assertRaises(ValueError):
                billing.merge_snapshot(old,invalid)

    def test_append_cli_queries_only_october_and_preserves_snapshots_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for provider in ['aws','gcp']:
                (root/f'{provider}.json').write_text(json.dumps(self.cloud_snapshot(provider)))
            before={p:p.read_bytes() for p in root.glob('*.json')}
            argv=['billing.py','extract','--months','1','--append','--end','2026-10-15',
                  '--aws-profile','billing','--gcp-table','project.dataset.table']
            with patch.object(billing,'DATA',root),patch.object(sys,'argv',argv),\
                 patch.object(billing,'build') as build,\
                 patch.object(billing,'aws_extract',return_value=([],self.cloud_snapshot()['meta'])) as aws,\
                 patch.object(billing,'gcp_extract',side_effect=ValueError('query failed')) as gcp:
                with self.assertRaises(SystemExit):
                    billing.main()
                self.assertEqual({p:p.read_bytes() for p in before},before)
                build.assert_not_called()
                self.assertEqual(aws.call_args.args[1:],('2026-10-01','2026-10-15',['2026-10']))
                self.assertEqual(gcp.call_args.args[1:],('2026-10-01','2026-10-15',['2026-10']))
                gcp.side_effect=None
                gcp.return_value=([],self.cloud_snapshot('gcp')['meta'])
                billing.main()
                self.assertEqual(build.call_args.args[0]['months'],['2026-09','2026-10'])
                for provider in ['aws','gcp']:
                    saved=json.loads((root/f'{provider}.json').read_text())
                    self.assertEqual(saved['records'],self.cloud_snapshot(provider)['records'][:1])

    def claude_file(self, directory, suffix='', amount='1.25', key='key-a', date='2026-08-15', model='Claude Sonnet'):
        path=Path(directory)/f'claude_api_cost_2026_08_01_to_2026_08_31{suffix}.csv'
        with path.open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=['usage_date_utc','model','workspace','cost_usd','cost_type','api_key_id'])
            writer.writeheader()
            writer.writerow(dict(usage_date_utc=date,model=model,workspace='production',cost_usd=amount,
                                 cost_type='token',api_key_id=key))
        return path

    def test_claude_duplicate_exports_and_distinct_service_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.claude_file(tmp)
            self.claude_file(tmp,' (1)',amount='2.50',key='service-account-b')
            self.claude_file(tmp,' (2)')
            snapshot=import_claude.import_reports([tmp])
            rows,meta=snapshot['records'],snapshot['meta']
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['amount'],3.75)
            self.assertEqual(rows[0]['entity_name'],'production')
            self.assertEqual(meta['covered_months'],['2026-08'])
            self.assertEqual(snapshot['months'],['2026-08'])
            self.assertEqual(len(meta['duplicate_files_skipped']),1)
            self.assertNotIn('key-a',json.dumps(rows))
            self.assertNotIn('service-account-b',json.dumps(meta))

    def test_claude_conflicting_overlapping_exports_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.claude_file(tmp)
            self.claude_file(tmp,' (1)',amount='9.00')
            with self.assertRaisesRegex(ValueError,'Overlapping Claude exports disagree'):
                import_claude.import_reports([tmp])

    def test_claude_exclusive_cutoff_and_non_model_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.claude_file(tmp,date='2026-08-31',model='--')
            snapshot=import_claude.import_reports([tmp])
            rows=billing.assemble({'claude':{**snapshot,'end':'2026-08-31'}})['records']
            self.assertEqual(rows,[])
            rows=billing.assemble({'claude':snapshot})['records']
            self.assertEqual(rows[0]['service'],'token')

    def test_claude_import_uses_cached_cloud_window_without_cloud_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.claude_file(tmp)
            snapshot=dict(schema_version=1,provider='aws',start='2026-08-01',end='2026-09-01',months=['2026-08'],
                          records=[],meta={},extracted_at='2026-09-01T00:00:00Z')
            (root/'aws.json').write_text(json.dumps(snapshot))
            with patch.object(billing,'DATA',root),patch.object(billing,'build') as build,\
                 patch.object(billing,'run_json',side_effect=AssertionError('Unexpected cloud call')),\
                 patch.object(sys,'argv',['import_claude.py',tmp]):
                import_claude.main()
            payload=build.call_args.args[0]
            self.assertEqual(payload['end'],'2026-09-01')
            self.assertEqual(set(payload['providers']),{'aws','claude'})
            self.assertFalse(payload['demo'])
            self.assertEqual(json.loads((root/'claude.json').read_text())['months'],['2026-08'])

    def test_default_includes_september(self):
        start, end, months = billing.window(6, today=dt.date(2026, 9, 30))
        self.assertEqual((start, end), ('2026-04-01', '2026-09-30'))
        self.assertEqual(months, ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09'])

    def test_year_boundary_and_explicit_cutoff(self):
        self.assertEqual(billing.window(6, today=dt.date(2026, 1, 1))[2],
                         ['2025-08', '2025-09', '2025-10', '2025-11', '2025-12', '2026-01'])
        self.assertEqual(billing.window(6, '2026-09-01', dt.date(2026, 9, 30))[2][-1], '2026-08')

    def test_decimal_grouping_signed_cost_and_currencies(self):
        def row(amount, currency='USD'):
            return dict(month='2026-09', entity_id='a', service='Compute', currency=currency, amount=amount)
        rows = billing.normalize([row('.1'), row('.2'), row('-.05'), row('9', 'EUR')], 'aws', ['2026-09'])
        self.assertEqual({r['currency']:r['amount'] for r in rows}, {'EUR':9, 'USD':.25})
        with self.assertRaises(ValueError):
            billing.normalize([row('NaN')], 'aws', ['2026-09'])

    def test_aws_pagination_account_names_and_estimates(self):
        def page(amount, token=None):
            return dict(NextPageToken=token, DimensionValueAttributes=[dict(Value='123', Attributes={'description':'Production'})],
                        ResultsByTime=[dict(TimePeriod={'Start':'2026-09-01'}, Estimated=True,
                                            Groups=[dict(Keys=['123','EC2'],Metrics={'UnblendedCost':{'Amount':amount,'Unit':'USD'}})])])
        args = argparse.Namespace(aws_metric='UnblendedCost',aws_profile='billing')
        with patch.object(billing,'run_json',side_effect=[page('10','next'),page('-2')]) as call:
            rows,meta=billing.aws_extract(args,'2026-04-01','2026-09-30',['2026-09'])
        self.assertEqual(rows[0]['amount'],8)
        self.assertEqual(rows[0]['entity_name'],'Production')
        self.assertEqual(meta['estimated_months'],['2026-09'])
        self.assertIn('--next-page-token',call.call_args_list[1].args[0])

    def test_gcp_query_preserves_credits_and_unallocated_rows(self):
        sql=billing.gcp_sql('my-project.billing.gcp_billing_export_v1_ABC','2026-04-01','2026-09-30')
        self.assertIn('UNNEST(credits)',sql)
        self.assertIn('__unallocated__',sql)
        self.assertIn("usage_start_time < TIMESTAMP('2026-09-30')",sql)
        self.assertNotIn('invoice.month',sql)
        with self.assertRaises(ValueError):
            billing.gcp_sql('table` UNION SELECT secret','2026-04-01','2026-09-30')

    def test_mismatched_snapshots_rejected(self):
        a=dict(schema_version=1,provider='aws',start='2026-04-01',end='2026-09-30',months=['2026-09'],records=[],meta={},extracted_at='now')
        g={**a,'provider':'gcp','end':'2026-09-29'}
        with self.assertRaises(ValueError):
            billing.assemble({'aws':a,'gcp':g})

    def test_offline_build_escapes_script_and_csv_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'dashboard.template.html').write_text('<style>__CSS__</style><script id="data" type="application/json">__DATA__</script><script>__JS__</script>')
            (root/'dashboard.css').write_text('body{}')
            (root/'dashboard.js').write_text('void 0;')
            payload=billing.sample(6)
            payload['records'][0]['entity_name']='</script><script>alert(1)</script>'
            payload['records'][0]['service']='=HYPERLINK("bad")'
            with patch.object(billing,'HERE',root),patch.object(billing,'DATA',root/'data'):
                billing.build(payload)
            html=(root/'cloud-cost-treemap.html').read_text()
            self.assertNotIn('</script><script>alert',html)
            self.assertIn('\\u003c/script',html)
            self.assertIn("'=HYPERLINK",(root/'data/costs.csv').read_text())
            self.assertEqual(json.loads((root/'data/costs.json').read_text())['records'][0]['entity_name'],payload['records'][0]['entity_name'])


if __name__=='__main__':
    unittest.main()

"""Prevent company research from inheriting index-only instructions."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import integrations
import news_evidence


class ResearchIdentityTests(unittest.TestCase):
    def research(self, ticker, fundamentals):
        create = AsyncMock()
        response = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            'signal': 'neutral', 'confidence': .5, 'fundamental_grades': {},
        })))])
        create.return_value = SimpleNamespace(parse=lambda: response, headers={})
        client = MagicMock()
        client.chat.completions.with_raw_response.create = create
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        settings = SimpleNamespace(openai_api_key='test', openai_base_url=None, openai_model='test')
        with patch.object(integrations, 'AsyncOpenAI', return_value=context):
            result = asyncio.run(integrations.OpenAIResearchAdapter(settings).research({
                'ticker': ticker, 'company_name': 'Advanced Micro Devices' if ticker == 'AMD' else ticker,
                'fundamentals': fundamentals,
                'headlines': [{'title': 'S&P 500 falls while AMD rises'}],
            }))
        return create.call_args.kwargs['messages'], result

    def test_amd_with_index_headline_receives_company_instructions(self):
        messages, result = self.research('AMD', {'total_revenue_ttm': 100, 'instrument_family': 'sp500'})
        # Identity follows the target symbol, not a headline or erroneous family label.
        self.assertIn('Assess this company', messages[0]['content'])
        self.assertNotIn('Company financial statement metrics are not applicable', messages[0]['content'])
        self.assertEqual(json.loads(messages[1]['content'])['assessment_target']['instrument_family'], 'company')
        self.assertTrue(result['assessment_target']['company_financials_applicable'])
        self.assertEqual(result['prompt_version'], news_evidence.RESEARCH_PROMPT_VERSION)

    def test_index_and_index_etfs_receive_only_index_instructions(self):
        for symbol in ['SPX500', 'SPY', 'VOO', 'IVV', '^GSPC']:
            with self.subTest(symbol=symbol):
                messages, result = self.research(symbol, {})
                self.assertIn('Company financial statement metrics are not applicable', messages[0]['content'])
                self.assertNotIn('Assess this company', messages[0]['content'])
                self.assertFalse(result['assessment_target']['company_financials_applicable'])

    def test_prompt_change_invalidates_neutral_reuse(self):
        old = news_evidence.neutral_fingerprint({'ticker': 'AMD'}, 'model')
        with patch.object(news_evidence, 'RESEARCH_PROMPT_VERSION', 'next-version'):
            self.assertNotEqual(old, news_evidence.neutral_fingerprint({'ticker': 'AMD'}, 'model'))

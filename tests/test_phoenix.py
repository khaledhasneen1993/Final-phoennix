"""Characterize the supplied Phoenix code without modifying its behavior."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('phoenix',Path(__file__).resolve().parents[1]/'final_phoenix.py')
phoenix=importlib.util.module_from_spec(spec);spec.loader.exec_module(phoenix)
S='BTC/USDT:USDT'

class PublicData:
    def __init__(self):self.price=100
    def amount_to_precision(self,symbol,amount):return round(amount,8)
    async def fetch_ticker(self,symbol):return {'last':self.price}
    async def fetch_order_book(self,symbol,limit):return {'bids':[[100,3]],'asks':[[101,1]]}
    async def fetch_ohlcv(self,symbol,timeframe,limit):
        rows=[[n,100,103,99,100,10] for n in range(limit)]
        rows[-1]=[limit,100,103,99,102,50]
        return rows

class PhoenixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.previous=phoenix.STATE_FILE
        phoenix.STATE_FILE=Path(self.tmp.name)/'state.json';self.data=PublicData()
        with patch.object(phoenix.ccxt,'binanceusdm',return_value=self.data):self.bot=phoenix.FinalPhoenix()
    async def asyncTearDown(self):phoenix.STATE_FILE=self.previous;self.tmp.cleanup()

    async def test_default_account_and_actual_margin_formula(self):
        self.assertEqual(self.bot.account.balance,100)
        self.assertAlmostEqual(self.bot.calc_margin_pct(.1),.09)
        self.assertAlmostEqual(self.bot.calc_margin_pct(.2),.1395)
        self.bot.survival_mode=True;self.assertAlmostEqual(self.bot.calc_margin_pct(.2),.0315)

    async def test_original_signal_thresholds(self):
        signal=await self.bot.detect(S)
        self.assertTrue(signal['long']);self.assertFalse(signal['short'])
        self.assertAlmostEqual(signal['strength'],.02*5*.5)
        self.bot.blacklist[S]=time.time()+60;self.assertIsNone(await self.bot.detect(S))

    async def test_hard_stop_precedes_retreat(self):
        self.assertTrue(await self.bot.enter(S,'buy',100,.1));self.data.price=94
        await self.bot.manage_positions()
        self.assertNotIn(S,self.bot.positions);self.assertNotIn(S,self.bot.account.positions)
        self.assertEqual(len(self.bot.account.trades),2)
        self.assertEqual(self.bot.account.trades[-1]['reason'],'SL')

    async def test_retreat_halves_then_new_stop_closes_remainder_if_price_stays(self):
        await self.bot.enter(S,'buy',100,.1);amount=self.bot.positions[S]['amount'];self.data.price=97
        await self.bot.manage_positions();p=self.bot.positions[S]
        self.assertTrue(p['retreated']);self.assertAlmostEqual(p['amount'],amount/2)
        self.assertAlmostEqual(self.bot.account.positions[S]['amount'],amount/2)
        self.assertAlmostEqual(p['sl'],98.8)
        self.assertEqual(self.bot.account.trades[-1]['reason'],'SMART_RETREAT')
        await self.bot.manage_positions();self.assertNotIn(S,self.bot.positions)
        self.assertEqual(self.bot.account.trades[-1]['reason'],'SL')

    async def test_trailing_both_sides_never_moves_back(self):
        for side,prices in [('buy',[102,103,102.8,102]),('sell',[98,97,97.2,98])]:
            with patch.object(phoenix.ccxt,'binanceusdm',return_value=self.data),patch.object(phoenix.FinalPhoenix,'load_state'):
                self.bot=phoenix.FinalPhoenix()
            self.assertTrue(await self.bot.enter(S,side,100,.1));stops=[]
            for price in prices[:3]:
                self.data.price=price;await self.bot.manage_positions();stops.append(self.bot.positions[S]['sl'])
            self.assertTrue(self.bot.positions[S]['trailing'])
            self.assertEqual(stops,sorted(stops,reverse=side=='sell'))
            self.data.price=prices[-1];await self.bot.manage_positions();self.assertNotIn(S,self.bot.positions)

    async def test_survival_enters_below_85_percent_and_exits_above_93(self):
        self.bot.account.balance=84;await self.bot.check_survival();self.assertTrue(self.bot.survival_mode)
        self.bot.account.balance=93;await self.bot.check_survival();self.assertTrue(self.bot.survival_mode)
        self.bot.account.balance=94;await self.bot.check_survival();self.assertFalse(self.bot.survival_mode)

    async def test_two_large_recorded_losses_arm_blacklist(self):
        for n in range(2):
            self.bot.last_trade.clear();await self.bot.enter(S,'buy',100,.1)
            await self.bot.close_full(S,'SL',94)
        self.assertTrue(self.bot.revenge_ready);self.assertEqual(self.bot.revenge_symbol,S)
        self.assertTrue(self.bot.is_blacklisted(S))

    async def test_state_keeps_120_executions_and_restores_saved_account(self):
        await self.bot.enter(S,'buy',100,.1)
        self.bot.account.trades*=130;self.bot.save_state()
        data=json.loads(phoenix.STATE_FILE.read_text());self.assertEqual(len(data['trades']),120)
        with patch.object(phoenix.ccxt,'binanceusdm',return_value=self.data):restored=phoenix.FinalPhoenix()
        self.assertEqual(restored.account.balance,self.bot.account.balance)
        self.assertEqual(restored.positions,self.bot.positions)

if __name__=='__main__':unittest.main()

import unittest, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blend_forecast
class TestBlend(unittest.TestCase):
    def test_implied_mu_centered(self):
        br=[{"kind":"B","floor":83,"cap":84,"yes_bid":10,"yes_ask":12},
            {"kind":"B","floor":84,"cap":85,"yes_bid":30,"yes_ask":32},
            {"kind":"B","floor":85,"cap":86,"yes_bid":30,"yes_ask":32},
            {"kind":"B","floor":86,"cap":87,"yes_bid":10,"yes_ask":12}]
        mu=blend_forecast.implied_mu(br); self.assertIsNotNone(mu); self.assertAlmostEqual(mu,85.0,delta=0.6)
    def test_implied_mu_thin_none(self):
        self.assertIsNone(blend_forecast.implied_mu([{"kind":"B","floor":1,"cap":2,"yes_bid":50,"yes_ask":52}]))
    def test_blend_high_conservative_ok(self):
        r=blend_forecast.blend_mu("high",85.0,84.0,82.0,None,"conservative")
        self.assertIsNotNone(r); mu,sg=r; self.assertTrue(60<mu<110); self.assertTrue(0.8<=sg<3.0)
    def test_blend_low_conservative_ok(self):
        r=blend_forecast.blend_mu("low",60.0,62.0,64.0,None,"conservative")
        self.assertIsNotNone(r)
    def test_failsafe_missing_market(self):
        self.assertIsNone(blend_forecast.blend_mu("high",None,84.0,82.0,None,"conservative"))
    def test_full_with_nwp_ok(self):
        nwp={"gfs_seamless":93.5,"ecmwf_ifs025":89.4,"icon_seamless":90.3,"gem_global":87.3,"jma_seamless":86.4,"ukmo_seamless":89.9}
        r=blend_forecast.blend_mu("high",90.0,89.0,86.0,nwp,"full")
        self.assertIsNotNone(r); self.assertTrue(60<r[0]<110)
    def test_failsafe_full_needs_nwp(self):
        # full variant without nwp models -> None (fail-safe), not a crash
        self.assertIsNone(blend_forecast.blend_mu("high",85.0,84.0,82.0,None,"full"))
if __name__=="__main__": unittest.main()

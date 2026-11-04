"""D060 mock diversity check: N synthetic personas through the REAL code path with a deterministic LAZY fake LLM.

    python -m gtgen.data.diversity_mock [--n 12]

STRATIFIED run: ``persona_strata`` -> ``persona_unique`` -> ``LLMRunners.persona`` (prompt rendering, schema validation,
band checks, retries) against ``LazyFakeClient``.  CONTROL run: the same fake LLM through the legacy path
(``LLMRunners.persona`` without strata, tau band by seed only, no uniqueness pass).

The fake behaves like a lazy LLM: inside a stated band it picks the centre or an edge (worst case for the spread,
never a random value); with no band it picks the same "typical" value for every trait; company names come from a
small pool containing deliberate near-duplicates so the regeneration loop is exercised.  This verifies the
stratifier / uniqueness code, NOT LLM mode collapse (that is checked on live output, D060 item 3).
OWNER: data-steward.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any, Optional, Sequence

from gtgen.contracts import Tier
from gtgen.eval.persona_diversity import DiversityReport, PersonaRecord, analyse, render
from gtgen.llm import LLMRunners
from gtgen.llm.schemas import PersonaOutputV3

CATEGORIES = ["COMMERCIAL", "PRODUCT", "SUPPLIER"]      # base; LOCATION added via with_location (D076)
BANDS = {"terms": {"budget_inr": {"kind": "numeric", "lo": 50000.0, "hi": 150000.0, "tight_end": "lo"}},
         "omitted": {}}
RUN_KEY = "diversity-mock"

#: worst case company pool: every name has a near-duplicate sibling (legal suffix / case differences), plus a few
#: unrelated names; the fake picks by hash(nonce) so collisions are frequent and deterministic.
COMPANY_POOL = [
    "Apex Hydro Systems Pvt Ltd", "Apex Hydro Systems Private Limited", "Bharat Flow Works", "Bharat Flow Works Ltd",
    "Kaveri Agro Tools", "Kaveri Agro Tools Pvt Ltd", "Orion Fabrication Co", "Orion Fabrication Company",
    "Sundar Textiles", "Meridian Cold Chain", "Tarang Electricals", "Vistara Packaging", "Neelam Plastics",
    "Rajdhani Castings", "Lotus Biotech Supplies", "Surya Polymers", "Indus Machine Works", "Pragati Auto Parts",
]
INDUSTRIES = ["manufacturing", "agriculture", "textiles", "food processing", "construction", "logistics"]
CITIES = ["Pune", "Surat", "Chennai", "Jaipur", "Kolkata", "Coimbatore", "Indore", "Nagpur"]
OPENERS = ["Formal and terse", "Chatty and warm", "Impatient", "Detail obsessed", "Calm and methodical", "Blunt"]
TONES = ["asks for specifics", "wants quick closure", "haggles politely", "double checks every figure"]
FORMALITY = ["formal", "semi-formal", "casual"]
VERBOSITY = ["terse", "balanced", "chatty"]
REGIONAL = ["Gujarati-English business register", "Tamil-English business register", "Hindi-English mix",
            "Bengali-English business register"]


def _h(*parts: Any) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def _lazy_pick(lo: float, hi: float, key: str) -> float:
    """Worst-case lazy choice inside [lo, hi]: the centre 60% of the time, otherwise an edge (hash of key)."""
    r = _h(key) % 10
    if r < 6:
        return (lo + hi) / 2
    return lo if r < 8 else hi


class LazyFakeClient:
    """Deterministic stand-in for ``LLMClient`` for the persona prompt (versions 3 and 4)."""

    strict = False

    def __init__(self) -> None:
        self.calls = 0

    def call(self, prompt_name, prompt_version, messages, response_model, temperature=None, seed=None,
             extra_inputs=None, context=None):
        assert prompt_name == "persona" and response_model is PersonaOutputV3
        self.calls += 1
        v = dict(extra_inputs or {})
        nonce = str(v["nonce"])
        guidance = v.get("strata_guidance") or ""
        bands = self._parse_bands(guidance)
        retried = len(messages) > 1                      # a rejected answer was appended: pick the centre safely
        if "tau" in bands:
            lo, hi = bands["tau"]
            tau = _lazy_pick(lo, hi, nonce + "tau") if not retried else (lo + hi) / 2
        else:
            m = re.search(r"between ([0-9.]+) and ([0-9.]+)", v["tau_guidance"])
            lo, hi = float(m.group(1)), float(m.group(2))
            tau = (lo + hi) / 2 + ((_h(nonce, "j") % 5) - 2) * 0.005       # centre +- tiny jitter
        tau = round(tau, 3)
        if tau in (0.3, 0.5, 0.7):
            tau = round(tau + 0.003, 3)

        def trait(name: str, default: float, as_int: bool = False):
            if name in bands:
                lo_, hi_ = bands[name]
                x = _lazy_pick(lo_, hi_, nonce + name)
                if retried:
                    x = (lo_ + hi_) / 2
                return int(round(x)) if as_int else round(x, 3)
            return default

        tol_bands = bands.get("tolerance", {})
        tol = []
        for c in json_list(v["categories_json"]):
            if c in tol_bands:
                lo_, hi_ = tol_bands[c]
                tol.append({"category": c, "value": round(_lazy_pick(lo_, hi_, nonce + "tol" + c), 3)})
            else:
                tol.append({"category": c, "value": round(2.5 + (_h(nonce, c) % 3) * 0.05, 3)})   # lazy typical value
        terms = {}
        for name, spec in (v["bands_json"].get("terms") or {}).items():
            terms[name] = round((spec["lo"] + spec["hi"]) / 2, 2)
        k = _h(nonce, "company")
        out = {
            "company_name": COMPANY_POOL[k % len(COMPANY_POOL)],
            "industry": INDUSTRIES[(k >> 8) % len(INDUSTRIES)], "city": CITIES[(k >> 16) % len(CITIES)],
            "style": f"{OPENERS[(k >> 24) % len(OPENERS)]}; {TONES[(k >> 32) % len(TONES)]}.",
            "tau": tau,
            "category_tolerance": [{"category": c, "multiplier": 1.0} for c in json_list(v["categories_json"])],
            "counter_band": 0.15, "patience_turns": 6, "max_concessions_per_turn": 2,
            "max_relaxations": trait("max_relaxations", 1, True), "patience_rounds": trait("patience_rounds", 2, True),
            "decision_randomness": trait("decision_randomness", 0.3), "tolerance": tol,
            "voice": {"formality": FORMALITY[(k >> 40) % 3], "verbosity": VERBOSITY[(k >> 44) % 3],
                      "regional_voice": REGIONAL[(k >> 48) % 4]},
            "sales_persona": {"speaking_style": "warm and plain spoken",
                              "sales_approach": "friendly and keen to close, states only facts"},
            "ranking_weights": {"spec_match": 0.3, "price_score": 0.25, "moq_fit": 0.1, "rating_bayes": 0.05,
                                "tenure_years": 0.05, "location_match": 0.10, "evidence_confidence": 0.15},
            "commercial_terms": terms,
        }
        return PersonaOutputV3.model_validate(out), "fake:" + hashlib.sha1(
            (nonce + str(len(messages))).encode()).hexdigest()[:12]

    @staticmethod
    def _parse_bands(guidance: str) -> dict:
        out: dict = {}
        num = r"(-?[0-9.]+)"
        for line in guidance.splitlines():
            m = re.match(rf"- (\w+): a value from {num} to {num}", line)
            if m:
                lo, hi = float(m.group(2)), float(m.group(3))
                out[m.group(1)] = (lo, hi)
            m = re.match(r"- tolerance \(value per category\): (.*)$", line)
            if m:
                out["tolerance"] = {}
                for part in m.group(1).split("; "):
                    mm = re.match(rf"(\w+): {num} to {num}", part)
                    out["tolerance"][mm.group(1)] = (float(mm.group(2)), float(mm.group(3)))
        return out


def json_list(x: Any) -> list:
    return list(x)


def _settings() -> dict:
    import yaml
    path = Path(__file__).resolve().parents[3] / "config" / "settings.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_personas(n: int = 12, *, stratified: bool = True, settings: Optional[dict] = None,
                   run_key: str = RUN_KEY) -> dict:
    """Build ``n`` personas through the real runner. Returns records, regeneration count and qa flags."""
    from gtgen.llm.strata import persona_strata, persona_unique, with_location
    settings = settings or _settings()
    cats = with_location(CATEGORIES, settings)
    runners = LLMRunners(LazyFakeClient(), settings)
    accepted: list[dict] = []
    records: list[PersonaRecord] = []
    regen, flags = 0, []
    for i in range(n):
        seed = i + 1
        common = dict(product_category="centrifugal pump", quantity=40.0, quantity_unit="pcs", budget_inr=None,
                      categories=cats, scenario_seed=seed, scenario_id=f"mock:s{seed}", bands=BANDS,
                      tightness_target=1)
        if stratified:
            strata = persona_strata(seed, n, run_key, settings, cats, index=i)
            res = persona_unique(runners, accepted, n=n, strata=strata, **common)
            regen += int((res.extra or {}).get("regenerations", 0))
        else:
            res = runners.persona(**common)
        if res.value is None:
            raise RuntimeError(f"persona {seed} failed: {res.qa_flags}")
        profile, persona = res.value
        flags += [f"s{seed}: {f}" for f in res.qa_flags]
        accepted.append({"company_name": profile.company_name, "industry": profile.industry, "city": profile.city})
        records.append(PersonaRecord(f"mock_s{seed}", persona, profile))
    tiers = {t.value: 0 for t in Tier}
    for r in records:
        tiers[r.persona.tier.value] += 1
    return {"records": records, "regenerations": regen, "qa_flags": flags, "tiers": tiers,
            "report": analyse(records, settings)}


def render_run(title: str, out: dict) -> str:
    rep: DiversityReport = out["report"]
    lines = [f"=== {title} ===", "tiers covered: " + ", ".join(f"{k}={v}" for k, v in out["tiers"].items()),
             f"company regenerations: {out['regenerations']}",
             f"company_not_unique flags: {[f for f in out['qa_flags'] if 'company_not_unique' in f]}",
             render(rep),
             "personas (tier tau pat rel rand | companies):"]
    for r in out["records"]:
        p = r.persona
        lines.append(f"  {r.scenario_id:<9}{p.tier.value:<14}tau={p.tau:.3f} pat={p.patience_rounds} "
                     f"rel={p.max_relaxations} rnd={p.decision_randomness} | {r.profile.company_name} "
                     f"({r.profile.industry}, {r.profile.city})")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gtgen.data.diversity_mock")
    ap.add_argument("--n", type=int, default=12)
    a = ap.parse_args(argv)
    settings = _settings()
    strat = build_personas(a.n, stratified=True, settings=settings)
    ctrl = build_personas(a.n, stratified=False, settings=settings)
    print(render_run(f"STRATIFIED (persona_strata + persona_unique), N={a.n}", strat))
    print()
    print(render_run(f"COLLAPSE CONTROL (legacy: tau band by seed only, no strata, no uniqueness), N={a.n}", ctrl))
    sr, cr = strat["report"], ctrl["report"]
    print(f"\nsummary: min distance stratified={sr.min_distance:.3f} vs control={cr.min_distance:.3f}; "
          f"mean {sr.mean_distance:.3f} vs {cr.mean_distance:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

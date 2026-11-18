from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ResearchInput(Strict):
    title: str = Field(default='Новые возможности', min_length=2, max_length=120)
    region: Literal['US', 'UK', 'EU', 'Global'] = 'US'
    count: int = Field(default=12, ge=10, le=15)
    budget: int = Field(default=10000, ge=100, le=1000000)
    team: str = Field(default='2 разработчика и основатель, B2B-продажи с нуля', min_length=5, max_length=500)
    focus: str = Field(default='Онлайн-сервисы для малого и среднего бизнеса', max_length=500)

class Candidate(Strict):
    name: str
    company: str
    description: str
    discovery_source_id: str
    query: str

class Discovery(Strict):
    candidates: list[Candidate]

class Claim(Strict):
    id: str
    statement: str
    source_id: str | None
    quote: str | None
    source_date: str | None
    method: Literal['primary_source', 'calculation', 'model_judgment', 'human_required']
    confidence: Literal['low', 'medium', 'high']
    rationale: str

class Gate(Strict):
    id: str
    status: Literal['pass', 'reject', 'review']
    reason: str
    claim_ids: list[str]

class Dimension(Strict):
    id: str
    score: int | None = Field(ge=0, le=5)
    reason: str
    claim_ids: list[str]

class Experiment(Strict):
    days: str
    action: str
    success: str
    stop: str

class Benchmark(Strict):
    company: str
    metric: Literal['valuation', 'market_cap', 'acquisition_value', 'funding_raised', 'revenue', 'unknown']
    amount_usd: float | None = Field(ge=0)
    claim_id: str | None

class Assessment(Strict):
    summary: str
    customer: str
    problem: str
    revenue: str
    analogue: str
    benchmark: Benchmark
    first_sale: str
    gates: list[Gate]
    claims: list[Claim]
    dimensions: list[Dimension]
    experiment: list[Experiment]
    unknowns: list[str]

class Risk(Strict):
    id: str
    title: str
    statement: str
    impact: Literal['high', 'medium', 'low']
    factual: bool
    search_query: str
    confidence: Literal['low', 'medium', 'high']
    method: Literal['primary_source', 'calculation', 'model_judgment', 'human_required']
    source_id: str | None
    quote: str | None
    source_date: str | None

class Critique(Strict):
    risks: list[Risk] = Field(min_length=1, max_length=3)

class CriterionImpact(Strict):
    criterion_id: str
    effect: Literal['contradicts', 'uncertain']
    reason: str

class Check(Strict):
    claim_id: str
    status: Literal['supported', 'refuted', 'partial', 'unknown', 'judgment']
    explanation: str
    source_id: str | None
    quote: str | None
    source_date: str | None
    confidence: Literal['low', 'medium', 'high']
    criterion_impacts: list[CriterionImpact] = Field(max_length=5)

class Verification(Strict):
    checks: list[Check]

class SearchPlan(Strict):
    queries: list[str] = Field(min_length=1,max_length=3)

class Decision(Strict):
    decision: Literal['shortlist', 'defer', 'dismiss'] | None
    note: str = Field(default='', max_length=2000)

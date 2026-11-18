from typing import Literal
from datetime import date
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator

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
    business_model_key: str | None = Field(default=None,max_length=160)

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


class LaunchEstimate(Strict):
    low: float | None = Field(ge=0,le=1000000)
    high: float | None = Field(ge=0,le=1000000)
    basis: str = Field(min_length=5,max_length=1200)
    method: Literal['model_judgment','calculation','human_required']
    confidence: Literal['low','medium','high']
    claim_ids: list[str] = Field(max_length=10)

    @model_validator(mode='after')
    def ordered(self):
        if (self.low is None)!=(self.high is None) or (self.low is not None and self.low>self.high):
            raise ValueError('Укажите упорядоченный диапазон или оставьте обе границы неизвестными')
        return self


class LaunchBrief(Strict):
    product: str = Field(min_length=5,max_length=900)
    first_customer: str = Field(min_length=3,max_length=900)
    acquisition_channel: str = Field(min_length=3,max_length=900)
    team: str = Field(min_length=3,max_length=600)
    budget_usd: LaunchEstimate
    first_payment_days: LaunchEstimate
    assumptions: list[str] = Field(min_length=1,max_length=8)

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
    launch_brief: LaunchBrief | None = None

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


class EvidenceActionInput(Strict):
    version: int = Field(ge=0)
    status: Literal['open','prepared']
    owner: str = Field(default='',max_length=120)
    note: str = Field(default='',max_length=2000)
    source_urls: list[str] = Field(default_factory=list,max_length=5)

    @field_validator('source_urls')
    @classmethod
    def urls(cls,values):
        for value in values:
            parsed=urlsplit(value)
            if len(value)>2000 or parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,80,443):
                raise ValueError('Нужна полная ссылка на публичный HTTP/HTTPS-источник')
        return list(dict.fromkeys(values))

    @model_validator(mode='after')
    def prepared_has_evidence(self):
        if self.status=='prepared' and not self.note.strip() and not self.source_urls:
            raise ValueError('Добавьте результат проверки или ссылку')
        return self


class PilotInput(Strict):
    version: int = Field(ge=0)
    status: Literal['planned','active','completed','stopped'] = 'planned'
    owner: str = Field(default='',max_length=120)
    hypothesis: str = Field(default='',max_length=1200)
    channel: str = Field(default='',max_length=800)
    start_date: date | None = None
    due_date: date | None = None
    budget_usd: float | None = Field(default=None,ge=0,le=1000000)
    success: str = Field(default='',max_length=1200)
    stop: str = Field(default='',max_length=1200)
    leads: int = Field(default=0,ge=0,le=100000000)
    conversations: int = Field(default=0,ge=0,le=100000000)
    paying_customers: int = Field(default=0,ge=0,le=100000000)
    payments: int = Field(default=0,ge=0,le=100000000)
    revenue_usd: float = Field(default=0,ge=0,le=100000000)
    spent_usd: float = Field(default=0,ge=0,le=100000000)
    result: str = Field(default='',max_length=3000)

    @model_validator(mode='after')
    def validate_plan(self):
        if self.start_date and self.due_date and not 0<=(self.due_date-self.start_date).days<=90:
            raise ValueError('Срок эксперимента должен составлять от 0 до 90 дней')
        if self.status!='planned':
            if not all(value.strip() for value in (self.owner,self.hypothesis,self.channel,self.success,self.stop)) or self.start_date is None or self.due_date is None or self.budget_usd is None:
                raise ValueError('Для запуска нужны ответственный, гипотеза, канал, даты, бюджет и критерии продолжения/остановки')
        if self.status in ('completed','stopped') and not self.result.strip():
            raise ValueError('Запишите фактический результат эксперимента')
        if self.revenue_usd>0 and (self.payments==0 or self.paying_customers==0):
            raise ValueError('При выручке укажите число оплат и платящих клиентов')
        return self


class OnboardingPreferences(Strict):
    onboarding_enabled: bool
    onboarding_seen_version: int = Field(ge=0,le=1)

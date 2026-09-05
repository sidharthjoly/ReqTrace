from .ashby import AshbyAdapter
from .eightfold import EightfoldAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .oracle import OracleAdapter
from .smartrecruiters import SmartRecruitersAdapter
from .workday import WorkdayAdapter

ADAPTERS = {a.vendor: a for a in (
    GreenhouseAdapter(), AshbyAdapter(), SmartRecruitersAdapter(),
    LeverAdapter(), WorkdayAdapter(), EightfoldAdapter(), OracleAdapter())}

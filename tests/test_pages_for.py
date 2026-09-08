from garden.store import Store
from garden.walkthrough import pages_for


def test_includes_costs_backlog_retro(garden):
    specs = pages_for(Store(garden), Store(garden).phase("demo", "p1"))
    urls = {spec.url for spec in specs}
    assert "/costs" in urls
    assert "/board?view=backlog" in urls
    assert "/phases/demo/p1/retro" in urls

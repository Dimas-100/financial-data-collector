import pytest

from financial_data_collector.connections import names


@pytest.mark.parametrize("raw, clean", [
    ("Checking ...1234", "Checking"), ("Visa x1234", "Visa"), ("Savings (1234)", "Savings"),
    ("Savings (...1234)", "Savings"), ("TOTAL CHECKING -1234", "TOTAL CHECKING"), ("Checking-1234", "Checking"),
    ("Card #1234", "Card"), ("Rewards ending in 1234", "Rewards"), ("Premier XXXX1234", "Premier"),
    ("Joint 000123456789", "Joint"), ("Sample Roth IRA (4321)", "Sample Roth IRA"), ("Everyday ...9876", "Everyday"),
    ("•••• 1234", "Account"), ("****1234", "Account"), ("x1234", "Account"), ("", "Account"), (None, "Account"),
    ("401k", "401k"), ("401(k) Plan", "401(k) Plan"), ("403(b)", "403(b)"), ("529 Plan", "529 Plan"),
    ("Flex1234", "Flex1234"), ("Account 12", "Account 12"), ("My 401 plan 4015", "My 401 plan"),
])
def test_digits_that_look_like_an_account_number_go(raw, clean):
    assert names.clean_name(raw) == clean


def test_label_is_institution_then_name_unless_the_name_already_starts_with_it():
    assert names.build_label("Example Bank", "Checking ...1234") == "Example Bank Checking"
    assert names.build_label("Example Bank", "Example Bank Savings") == "Example Bank Savings"
    assert names.build_label("", "Roth IRA") == "Roth IRA"
    assert names.build_label(None, "") == "Account"
    assert names.build_label("Example Bank", "x1234") == "Example Bank Account"
    assert len(names.build_label("A" * 60, "B" * 60)) <= names.MAX_LABEL


def test_institution_code_is_the_form_the_accounts_table_uses():
    assert names.institution_code("Fidelity") == "fidelity"
    assert names.institution_code("Example Bank & Trust") == "example_bank_trust"
    assert names.institution_code("") == "unknown"


@pytest.mark.parametrize("text, kind", [
    ("Home Mortgage", "mortgage"), ("HELOC", "line_of_credit"), ("Auto Loan", "loan"), ("Debit Card Checking", "checking"),
    ("Credit Union Savings", "savings"), ("Money Market Plus", "money_market"), ("Cash Rewards Visa", "credit_card"),
    ("Platinum Card", "credit_card"), ("Discover Savings", "savings"), ("Crypto", "crypto"), ("Roth IRA", "roth_ira"),
    ("Rollover IRA", "traditional_ira"), ("Traditional IRA", "traditional_ira"), ("My 401(k)", "401k"),
    ("Individual", "brokerage"), ("Investment Account", "brokerage"), ("Joint TOD", "brokerage"),
    ("Everyday", None), ("Miranda", None), ("", None), (None, None),
])
def test_kind_rules_top_to_bottom(text, kind):
    assert names.match_kind(text) == kind


def test_slug_and_key():
    assert names.slug_for("roth_ira") == "roth" and names.slug_for("brokerage") == "brokerage"
    assert names.slug_for("checking") == "other"
    k = names.external_key("simplefin", "ACT-1")
    assert len(k) == 64 and k == names.external_key("simplefin", "ACT-1") != names.external_key("snaptrade", "ACT-1")
    assert "ACT-1" not in k
    assert "credit_card" in names.KINDS and "other" in names.KINDS and names.DEBT_KINDS <= set(names.KINDS)

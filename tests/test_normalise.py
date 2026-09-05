from reqtrace.normalise import html_to_text, parse_location, sanitise_html


def test_australian_shapes():
    assert parse_location("Sydney")[:2] == ("Sydney", "AU")
    assert parse_location("Sydney, NSW")[:2] == ("Sydney", "AU")
    assert parse_location("Sydney, New South Wales, Australia")[:2] == ("Sydney", "AU")
    assert parse_location("AU-NSW-Sydney")[:2] == ("Sydney", "AU")


def test_multi_city_picks_a_filterable_city():
    city, country, _ = parse_location("Sydney or Melbourne")
    assert city == "Sydney" and country == "AU"


def test_remote_is_separated_from_hybrid():
    """Separating true remote from hybrid-labelled-remote is the highest-value
    filter, so 'Hybrid Remote' must not read as fully remote."""
    assert parse_location("Remote - Australia")[2] == "remote"
    assert parse_location("Hybrid Remote — Sydney")[2] == "hybrid"
    assert parse_location("Sydney (Hybrid)")[2] == "hybrid"


def test_non_australian_locations():
    assert parse_location("London, England, United Kingdom")[1] == "GB"
    assert parse_location("Austin, Texas, United States")[1] == "US"


def test_blank_location():
    assert parse_location("") == (None, None, "unknown")


def test_sanitiser_drops_scripts_keeps_structure():
    dirty = "<p>Hi</p><script>alert(1)</script><a href='x' onclick='y()'>l</a>"
    clean = sanitise_html(dirty)
    assert "<p>" in clean and "script" not in clean.lower() and "onclick" not in clean


def test_entity_encoded_html_is_decoded():
    assert "<p>" in sanitise_html("&lt;p&gt;Hello&lt;/p&gt;")
    assert html_to_text("&lt;p&gt;Hello&lt;/p&gt;").strip() == "Hello"


def test_bare_state_codes_are_not_evidence_of_australia():
    """'SA' is South Africa as often as South Australia, and 'WA' is Washington.
    Guessing AU from a bare state code pollutes the AU filter."""
    assert parse_location("SA - Remote - SA")[1] != "AU"
    assert parse_location("Remote (South Africa)")[1] != "AU"
    # An explicit AU marker still resolves.
    assert parse_location("AU - HQ - NSW")[1] == "AU"
    assert parse_location("AU - Remote - VIC")[:3] == (None, "AU", "remote")


def test_ambiguous_city_names_need_explicit_au_evidence():
    """Newcastle is also England and Perth is also Scotland. Treating the bare
    name as Australian makes UK boards look local."""
    from reqtrace.normalise import is_australian

    for raw in ("Newcastle", "Perth", "Richmond", "Hamilton"):
        city, country, _ = parse_location(raw)
        assert not is_australian(city, country, raw), raw
    # With explicit evidence they resolve normally.
    for raw in ("Newcastle, NSW, Australia", "Perth, Australia", "AU - Perth"):
        city, country, _ = parse_location(raw)
        assert is_australian(city, country, raw), raw


def test_unambiguous_au_cities_still_resolve():
    for raw in ("Sydney", "Melbourne", "Brisbane", "Adelaide", "Canberra"):
        city, country, _ = parse_location(raw)
        from reqtrace.normalise import is_australian
        assert is_australian(city, country, raw), raw


def test_workday_multi_location_placeholder_is_not_a_city():
    """Workday says '2 Locations' for a multi-site posting. It is not a city,
    and it was showing up as one in the UI."""
    assert parse_location("2 Locations") == (None, None, "unknown")
    assert parse_location("12 locations")[0] is None


def test_state_codes_do_not_become_cities():
    """CommBank's Workday board lists roles as 'NSW/ ACT Region'; that was
    landing in the UI as a city called 'Nsw'."""
    assert parse_location("NSW/ ACT Region")[0] is None
    assert parse_location("VIC CBD Melbourne Area")[0] == "Melbourne"

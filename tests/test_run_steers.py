"""Steers the operator queued but the session never took."""


def _undelivered_card(html: str) -> str:
    """The 'Undelivered' card, or '' when the page does not show one."""
    i = html.find("Undelivered")
    if i < 0:
        return ""
    end = html.find("</section>", i)
    return html[i:end if end > 0 else len(html)]


def test_finished_run_offers_undelivered_steers_back(client, fresh_db):
    fresh_db.upsert_item("may", "issue", 11, "Footer", "alice", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#11", "fix", "m", "Malcolm")
    client.post(f"/run/{rid}/steer", data={"text": "use the stamp file"})
    client.post(f"/run/{rid}/steer", data={"text": "skip the git probe"})

    # the run ends without the SDK loop ever calling take_steers
    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    assert all(s["delivered_at"] is None for s in fresh_db.run_steers(rid))

    # both are listed as undelivered, not quietly shown as if they landed
    card = _undelivered_card(client.get(f"/run/{rid}").text)
    assert "use the stamp file" in card and "skip the git probe" in card

    # and the operator is told in the project events
    assert any("undeliver" in e["message"].lower()
               for e in fresh_db.recent_events(20, "may"))

    ids = [s["id"] for s in fresh_db.run_steers(rid)]

    # keeping one turns it into a direction on the item
    client.post(f"/run/{rid}/steer/{ids[0]}/keep")
    assert any("use the stamp file" in d["question"]
               for d in fresh_db.pending_directives("may"))

    # discarding the other drops it
    client.post(f"/run/{rid}/steer/{ids[1]}/discard")

    html = client.get(f"/run/{rid}").text
    card = _undelivered_card(html)
    assert "use the stamp file" not in card and "skip the git probe" not in card
    assert "skip the git probe" not in html          # discarded, hidden

    # neither can be resurrected into a later session
    assert fresh_db.take_steers(rid) == []


def test_keep_does_not_duplicate_the_thread_line(client, fresh_db):
    """The steer box already mirrors the text into the item thread."""
    fresh_db.upsert_item("may", "issue", 12, "Footer", "alice", "open", "x")
    rid = fresh_db.start_run("may", "ic", "issue#12", "fix", "m", "Malcolm")
    client.post(f"/run/{rid}/steer", data={"text": "use the stamp file"})
    fresh_db.finish_run(rid, True, 0.1, 1, "done")
    sid = fresh_db.run_steers(rid)[0]["id"]

    client.post(f"/run/{rid}/steer/{sid}/keep")

    lines = [t for t in fresh_db.thread("may", "issue#12")
             if "use the stamp file" in t["text"]]
    assert len(lines) == 1 and "mid-run" in lines[0]["text"]
    # settled either way: the next session can't be handed it again
    assert fresh_db.take_steers(rid) == []
    assert fresh_db.undelivered_steers(rid) == []


def test_projectless_run_offers_discard_only(client, fresh_db):
    rid = fresh_db.start_run("", "standup", "", "standup", "m", "Harry")
    client.post(f"/run/{rid}/steer", data={"text": "keep it short"})
    fresh_db.finish_run(rid, True, 0.1, 1, "done")

    card = _undelivered_card(client.get(f"/run/{rid}").text)
    assert "keep it short" in card
    assert "Send as direction" not in card and "Discard" in card

    # keeping is not on offer, and asking for it anyway changes nothing
    sid = fresh_db.run_steers(rid)[0]["id"]
    assert client.post(f"/run/{rid}/steer/{sid}/keep").status_code == 200
    assert len(fresh_db.undelivered_steers(rid)) == 1


def test_unknown_ids_are_no_ops(client, fresh_db):
    rid = fresh_db.start_run("may", "ic", "issue#13", "fix", "m", "Malcolm")
    client.post(f"/run/{rid}/steer", data={"text": "mind the cache"})
    other = fresh_db.start_run("may", "ic", "issue#14", "fix", "m", "Malcolm")
    sid = fresh_db.run_steers(rid)[0]["id"]

    for path in (f"/run/{rid}/steer/9999/keep", f"/run/{rid}/steer/9999/discard",
                 f"/run/{other}/steer/{sid}/discard"):
        assert client.post(path).status_code == 200   # 303 then the run page
    assert len(fresh_db.undelivered_steers(rid)) == 1

from __future__ import annotations

from typing import Any

from datetime import datetime

import json
import time
import os

from aiohttp.web import Request, Response, HTTPNotFound, HTTPForbidden, HTTPUnauthorized, HTTPNotImplemented, FileField

import aiohttp_jinja2

from nameinternal import get_all_relics, get_all_cards, get_all_events, get_relic, get_event
from sts_profile import get_profile
from gamedata import FileParser, generate_graph
from webpage import router
from logger import logger
from events import add_listener

import config

__all__ = ["get_latest_run"]

_cache: dict[str, RunParser] = {}
_ts_cache: dict[int, RunParser] = {}

def get_latest_run(character: str | None, victory: bool | None) -> RunParser:
    _update_cache()
    latest = _ts_cache[max(_ts_cache)]
    key = "prev"
    if character is not None:
        key = "prev_char"
        while latest.character != character:
            latest = latest.matched["prev"]

    if victory is not None:
        if victory:
            while not latest.won:
                latest = latest.matched[key]
        else:
            while latest.won:
                latest = latest.matched[key]

    return latest

class RunParser(FileParser):
    def __init__(self, filename: str, profile: int, data: dict[str, Any]):
        if filename in _cache:
            raise RuntimeError(f"Created duplicate run parser with name {filename}")
        super().__init__(data)
        self.filename = filename
        self.name, _, ext = filename.partition(".")
        self.matched: dict[str, RunParser] = {}
        self._character = data["character_chosen"]
        self._profile = profile

    @property
    def display_name(self) -> str:
        return f"({self.character} {'victory' if self.won else 'loss'}) {self.timestamp}"

    @property
    def profile(self):
        return get_profile(self._profile)

    @property
    def timestamp(self) -> str:
        return datetime.fromtimestamp(self.data["timestamp"]).isoformat(" ")

    @property
    def won(self) -> bool:
        return self.data["victory"]

    @property
    def killed_by(self) -> str | None:
        return self.data.get("killed_by")

    @property
    def floor_reached(self) -> int:
        return int(self["floor_reached"])

    @property
    def final_health(self) -> tuple[int, int]:
        return self["current_hp_per_floor"][-1], self["max_hp_per_floor"][-1]

    @property
    def score(self) -> int:
        return int(self.data["score"])

    @property
    def run_length(self) -> str:
        seconds = self.data["playtime"]
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:>02}:{seconds:>02}"
        return f"{minutes:>02}:{seconds:>02}"

@add_listener("setup_init")
async def _setup_cache():
    _update_cache()
    for i in range(3):
        os.makedirs(os.path.join("data", "runs", str(i)), exist_ok=True)

def _update_cache():
    start = time.time()
    for path, folders, files in os.walk(os.path.join("data", "runs")):
        for folder in folders:
            profile = int(folder)
            _cur_cache: dict[int, RunParser] = {}
            for p1, d1, f1 in os.walk(os.path.join(path, folder)):
                for file in f1:
                    if file in _cache:
                        parser = _cache[file]
                    else:
                        with open(os.path.join(p1, file)) as f:
                            _cache[file] = parser = RunParser(file, profile, json.load(f))
                            _ts_cache[parser.data["timestamp"]] = parser
                    _cur_cache[parser.data["timestamp"]] = parser

            prev = None
            prev_char: dict[str, RunParser | None] = {}
            prev_win = None
            prev_loss = None

            for t in sorted(_cur_cache):
                cur = _cur_cache[t]
                if prev is not None:
                    if "prev" not in cur.matched:
                        prev.matched["next"] = cur
                        cur.matched["prev"] = prev
                    if cur.character not in prev_char:
                        prev_char[cur.character] = None
                    if "prev_char" not in cur.matched and (c := prev_char[cur.character]) is not None:
                        c.matched["next_char"] = cur
                        cur.matched["prev_char"] = c
                    prev_char[cur.character] = cur
                    if cur.won:
                        if "prev_win" not in cur.matched and prev_win is not None:
                            prev_win.matched["next_win"] = cur
                            cur.matched["prev_win"] = prev_win
                        prev_win = cur
                    else:
                        if "prev_loss" not in cur.matched and prev_loss is not None:
                            prev_loss.matched["next_loss"] = cur
                            cur.matched["prev_loss"] = prev_loss
                        prev_loss = cur
                prev = cur

    # I don't actually know how long this cache updating is going to take...
    # I think it's as optimized as I could make it while still being safe,
    # but it's possible it still takes some time. I'm not going to focus on
    # that for now, but logging the update time everytime, in case it turns
    # out to be a bottleneck. We only want to actually update new runs.
    logger.info(f"Updated run parser cache in {time.time() - start}s")

@router.get("/profile/{profile}/runs")
@aiohttp_jinja2.template("runs.jinja2")
async def runs_page(req: Request):
    p = req.match_info["profile"]
    try:
        p = int(p)
        prof = get_profile(p)
    except KeyError:
        raise HTTPNotFound()
    except ValueError:
        raise HTTPForbidden(reason="profile must be integer")
    _update_cache()
    runs = [_ts_cache[t] for t in sorted(_ts_cache) if _ts_cache[t]._profile == p]
    runs.reverse() # return most recent runs at the top
    return {"runs": runs, "profile": prof}

@router.get("/runs")
@aiohttp_jinja2.template("runs_profile.jinja2")
async def pick_profile(req: Request):
    profiles = []
    for i in range(3):
        try:
            profiles.append(get_profile(i))
        except KeyError:
            continue

    if not profiles:
        raise HTTPNotImplemented(reason="No run files were found")

    return {"profiles": profiles}

def _get_parser(name) -> RunParser | None:
    parser = _cache.get(f"{name}.run") # most common case
    if parser is None:
        _update_cache()
        parser = _cache.get(f"{name}.run") # try again, just in case
        if parser is None: # okay, iterate through everything
            for run_parser in _cache.values():
                if run_parser.name == name:
                    parser = run_parser
                    break

    return parser

def _truthy(x: str | None) -> bool:
    if x and x.lower() in ("1", "true", "yes"):
        return True
    return False

def _falsey(x: str | None) -> bool:
    if x and x.lower() in ("0", "false", "no"):
        return False
    return True

@router.get("/runs/{name}")
@aiohttp_jinja2.template("run_single.jinja2")
async def run_single(req: Request):
    parser = _get_parser(req.match_info["name"])
    if parser is None:
        raise HTTPNotFound()
    embed = _falsey(req.query.get("embed"))
    redirect = _truthy(req.query.get("redirect"))
    return {"parser": parser, "embed": embed, "redirect": redirect}

@router.get("/runs/{name}/raw")
async def run_raw_json(req: Request) -> Response:
    parser = _get_parser(req.match_info["name"])
    if parser is None:
        raise HTTPNotFound()

    return Response(text=json.dumps(parser.data, indent=4), content_type="application/json")

@router.get("/runs/{name}/{type}")
async def run_chart(req: Request) -> Response:
    parser = _get_parser(req.match_info["name"])
    if parser is None:
        raise HTTPNotFound()

    return generate_graph(parser, req.match_info["type"], req.query, req.query_string)

#@router.get("/compare")
@aiohttp_jinja2.template("runs_compare.jinja2")
async def compare_choose(req: Request):
    return {
        "characters": ("Ironclad", "Silent", "Defect", "Watcher"),
        "relics": get_all_relics(),
        "cards": get_all_cards(),
    }

#@router.get("/compare/view")
@aiohttp_jinja2.template("compare_single.jinja2")
async def compare_runs(req: Request):
    context = {}
    try:
        start = int(req.query.get("start", 0))
        end = int(req.query.get("end", time.time()))
        score = int(req.query.get("score", 0))
    except ValueError:
        raise HTTPForbidden(reason="'start', 'end', 'score' params must be integers if present")

    chars = req.query.getall("character", [])
    victory = _truthy(req.query.get("victory"))
    loss = _falsey(req.query.get("loss"))
    relics = req.query.getall("relic", [])
    cards = req.query.getall("card", [])

    return context

@router.post("/sync/run")
async def receive_run(req: Request) -> Response:
    pw = req.query.get("key")
    if pw is None:
        raise HTTPUnauthorized(reason="No API key provided")
    if not config.secret:
        raise HTTPNotImplemented(reason="No API key present in config")
    if pw != config.secret:
        raise HTTPForbidden(reason="Invalid API key provided")

    post = await req.post()

    content = post.get("run")
    if isinstance(content, FileField):
        content = content.file.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8", "xmlcharrefreplace")

    name = post.get("name")
    if isinstance(name, FileField):
        name = name.file.read()
    if isinstance(name, bytes):
        name = name.decode("utf-8", "xmlcharrefreplace")

    profile = post.get("profile")
    if isinstance(profile, FileField):
        profile = profile.file.read()
    if isinstance(profile, bytes):
        profile = profile.decode("utf-8", "xmlcharrefreplace")

    with open(os.path.join("data", "runs", profile, name), "w") as f:
        f.write(content)
    data = json.loads(content)
    if name not in _cache:
        _cache[name] = parser = RunParser(name, int(profile), data)
        _ts_cache[parser.data["timestamp"]] = parser
        _update_cache()

    logger.debug(f"Received run history file. Updated data. Transaction time: {time.time() - float(req.query['start'])}s")

    return Response()

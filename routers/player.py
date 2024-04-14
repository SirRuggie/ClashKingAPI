import datetime
import re
import time
import asyncio
import pendulum as pend
import coc
import aiohttp

from collections import defaultdict, deque
from fastapi import Request, Response, HTTPException, Query
from fastapi import APIRouter
from fastapi_cache.decorator import cache
from typing import List, Annotated
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from utils.utils import fix_tag, redis, db_client
from datetime import timedelta

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(tags=["Player Endpoints"])


@router.get("/player/{player_tag}/stats",
         name="All collected Stats for a player (clan games, looted, activity, etc)")
@cache(expire=300)
@limiter.limit("30/second")
async def player_stat(player_tag: str, request: Request, response: Response):
    player_tag = player_tag and "#" + re.sub(r"[^A-Z0-9]+", "", player_tag.upper()).replace("O", "0")
    result = await db_client.player_stats_db.find_one({"tag": player_tag})
    lb_spot = await db_client.player_leaderboard_db.find_one({"tag": player_tag})

    if result is None:
        raise HTTPException(status_code=404, detail=f"No player found")
    try:
        del result["legends"]["streak"]
    except:
        pass
    result = {
        "name" : result.get("name"),
        "tag" : result.get("tag"),
        "townhall" : result.get("townhall"),
        "legends" : result.get("legends", {}),
        "last_online" : result.get("last_online"),
        "looted" : {"gold": result.get("gold", {}), "elixir": result.get("elixir", {}), "dark_elixir": result.get("dark_elixir", {})},
        "trophies" : result.get("trophies", 0),
        "warStars" : result.get("warStars"),
        "clanCapitalContributions" : result.get("aggressive_capitalism"),
        "donations": result.get("donations", {}),
        "capital" : result.get("capital_gold", {}),
        "clan_games" : result.get("clan_games", {}),
        "season_pass" : result.get("season_pass", {}),
        "attack_wins" : result.get("attack_wins", {}),
        "activity" : result.get("activity", {}),
        "clan_tag" : result.get("clan_tag"),
        "league" : result.get("league")
    }

    if lb_spot is not None:
        try:
            result["legends"]["global_rank"] = lb_spot["global_rank"]
            result["legends"]["local_rank"] = lb_spot["local_rank"]
        except:
            pass
        try:
            result["location"] = lb_spot["country_name"]
        except:
            pass

    return result


@router.get("/player/{player_tag}/legends",
         name="Legend stats for a player")
@cache(expire=300)
@limiter.limit("30/second")
async def player_legend(player_tag: str, request: Request, response: Response, season: str = None):
    player_tag = fix_tag(player_tag)
    c_time = time.time()
    result = await db_client.player_stats_db.find_one({"tag": player_tag}, projection={"name" : 1, "townhall" : 1, "legends" : 1, "tag" : 1})
    if result is None:
        raise HTTPException(status_code=404, detail=f"No player found")
    ranking_data = await db_client.player_leaderboard_db.find_one({"tag": player_tag}, projection={"_id" : 0})

    default = {"country_code": None,
               "country_name": None,
               "local_rank": None,
               "global_rank": None}
    if ranking_data is None:
        ranking_data = default
    if ranking_data.get("global_rank") is None:
        self_global_ranking = await db_client.legend_rankings.find_one({"tag": player_tag})
        if self_global_ranking:
            ranking_data["global_rank"] = self_global_ranking.get("rank")

    legend_data = result.get('legends', {})
    if season and legend_data != {}:
        year, month = season.split("-")
        season_start = coc.utils.get_season_start(month=int(month) - 1, year=int(year))
        season_end = coc.utils.get_season_end(month=int(month) - 1, year=int(year))
        delta = season_end - season_start
        days = [season_start + timedelta(days=i) for i in range(delta.days)]
        days = [day.strftime("%Y-%m-%d") for day in days]

        _holder = {}
        for day in days:
            _holder[day] = legend_data.get(day, {})
        legend_data = _holder

    result = {
        "name" : result.get("name"),
        "tag" : result.get("tag"),
        "townhall" : result.get("townhall"),
        "legends" : legend_data,
        "rankings" : ranking_data
    }

    result["legends"].pop("global_rank", None)
    result["legends"].pop("local_rank", None)
    result["streak"] = result["legends"].pop("streak", 0)
    return dict(result)


@router.get("/player/{player_tag}/historical/{season}",
         name="Historical data for player events")
@cache(expire=300)
@limiter.limit("30/second")
async def player_historical(player_tag: str, season:str, request: Request, response: Response):
    player_tag = player_tag and "#" + re.sub(r"[^A-Z0-9]+", "", player_tag.upper()).replace("O", "0")
    year = season[:4]
    month = season[-2:]
    season_start = coc.utils.get_season_start(month=int(month) - 1, year=int(year))
    season_end = coc.utils.get_season_end(month=int(month) - 1, year=int(year))
    historical_data = await db_client.player_history.find({"$and" : [{"tag": player_tag}, {"time" : {"$gte" : season_start.timestamp()}}, {"time" : {"$lte" : season_end.timestamp()}}]}).sort("time", 1).to_list(length=25000)
    breakdown = defaultdict(list)
    for data in historical_data:
        del data["_id"]
        breakdown[data["type"]].append(data)

    result = {}
    for key, item in breakdown.items():
        result[key] = item

    return dict(result)


@router.get("/player/{player_tag}/warhits",
         name="War attacks done/defended by a player")
@cache(expire=300)
@limiter.limit("30/second")
async def player_warhits(player_tag: str, request: Request, response: Response, timestamp_start: int = 0, timestamp_end: int = 2527625513, limit: int = 50):
    client = coc.Client(raw_attribute=True)
    player_tag = fix_tag(player_tag)
    pend.from_timestamp(timestamp_start, tz=pend.UTC)
    START = pend.from_timestamp(timestamp_start, tz=pend.UTC).strftime('%Y%m%dT%H%M%S.000Z')
    END = pend.from_timestamp(timestamp_end, tz=pend.UTC).strftime('%Y%m%dT%H%M%S.000Z')
    pipeline = [
        {"$match": {"$or": [{"data.clan.members.tag": player_tag}, {"data.opponent.members.tag": player_tag}]}},
        {"$match" : {"$and" : [{"data.preparationStartTime" : {"$gte" : START}}, {"data.preparationStartTime" : {"$lte" : END}}]}},
        {"$unset": ["_id"]},
        {"$project": {"data": "$data"}},
        {"$sort" : {"data.preparationStartTime" : -1}}
    ]
    wars = await db_client.clan_wars.aggregate(pipeline, allowDiskUse=True).to_list(length=None)
    found_wars = set()
    stats = {"items" : []}
    local_limit = 0
    for war in wars:
        war = war.get("data")
        war = coc.ClanWar(data=war, client=client)
        war_unique_id = "-".join(sorted([war.clan_tag, war.opponent.tag])) + f"-{int(war.preparation_start_time.time.timestamp())}"
        if war_unique_id in found_wars:
            continue
        found_wars.add(war_unique_id)
        if limit == local_limit:
            break
        local_limit += 1

        war_member = war.get_member(player_tag)

        war_data: dict = war._raw_data
        war_data.pop("status_code", None)
        war_data.pop("_response_retry", None)
        war_data.pop("timestamp", None)
        war_data.pop("timestamp", None)
        del war_data["clan"]["members"]
        del war_data["opponent"]["members"]
        war_data["type"] = war.type

        member_raw_data = war_member._raw_data
        member_raw_data.pop("bestOpponentAttack", None)
        member_raw_data.pop("attacks", None)

        done_holder = {
            "war_data": war_data,
            "member_data" : member_raw_data,
            "attacks": [],
            "defenses" : []
        }
        for attack in war_member.attacks:
            raw_attack: dict = attack._raw_data
            raw_attack["fresh"] = attack.is_fresh_attack
            defender_raw_data = attack.defender._raw_data
            defender_raw_data.pop("attacks", None)
            defender_raw_data.pop("bestOpponentAttack", None)
            raw_attack["defender"] = defender_raw_data
            raw_attack["attack_order"] = attack.order
            done_holder["attacks"].append(raw_attack)

        for defense in war_member.defenses:
            raw_defense: dict = defense._raw_data
            raw_defense["fresh"] = defense.is_fresh_attack

            defender_raw_data = defense.defender._raw_data
            defender_raw_data.pop("attacks", None)
            defender_raw_data.pop("bestOpponentAttack", None)

            raw_defense["defender"] = defender_raw_data
            raw_defense["attack_order"] = defense.order
            done_holder["defenses"].append(raw_defense)

        stats["items"].append(done_holder)
    print(stats)
    return stats



'''@router.get("/player/to-do",
             name="To-do list for player(s)")
@limiter.limit("10/second")
async def player_to_do(players: Annotated[List[str], Query(min_length=1, max_length=50)]):
    pass'''





@router.get("/player/{player_tag}/legend_rankings",
         name="Previous player legend rankings")
@cache(expire=300)
@limiter.limit("30/second")
async def player_legend_rankings(player_tag: str, request: Request, response: Response, limit:int = 10):

    player_tag = fix_tag(player_tag)
    results = await db_client.legend_history.find({"tag": player_tag}).sort("season", -1).limit(limit).to_list(length=None)
    for result in results:
        del result["_id"]

    return results


@router.get("/player/{player_tag}/wartimer",
         name="Get the war timer for a player")
@cache(expire=300)
@limiter.limit("30/second")
async def player_wartimer(player_tag: str, request: Request, response: Response):
    player_tag = fix_tag(player_tag)
    result = await db_client.war_timer.find_one({"_id" : player_tag})
    if result is None:
        return result
    result["tag"] = result.pop("_id")
    time: datetime.datetime = result["time"]
    time = time.replace(tzinfo=pend.UTC)
    result["unix_time"] = time.timestamp()
    result["time"] = time.isoformat()
    return result


@router.get("/player/search/{name}",
         name="Search for players by name")
@cache(expire=300)
@limiter.limit("30/second")
async def search_players(name: str, request: Request, response: Response):
    pipeline = [
        {
            "$search": {
                "index": "player_search",
                "autocomplete": {
                    "query": name,
                    "path": "name",
                },
            }
        },
        {"$limit": 25}
    ]
    results = await db_client.player_search.aggregate(pipeline=pipeline).to_list(length=None)
    for result in results:
        del result["_id"]
    return {"items" : results}


@router.post("/player/bulk",
          name="Cached endpoint response (bulk fetch)",
          include_in_schema=False)
@limiter.limit("15/second")
async def player_bulk(player_tags: List[str], api_keys: List[str], request: Request, response: Response):
    async def get_player_responses(keys: deque, tags: list[str]):
        tasks = []
        connector = aiohttp.TCPConnector(limit=2000, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=1800)
        cached_responses = await redis.mget(keys=player_tags)
        tag_map = {tag: r for tag, r in zip(tags, cached_responses)}

        missing_tags = [t for t, r in tag_map.items() if r is None]
        results = []
        if missing_tags:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                for tag in missing_tags:
                    keys.rotate(1)
                    async def fetch(url, session: aiohttp.ClientSession, headers: dict, tag: str):
                        async with session.get(url, headers=headers) as new_response:
                            if new_response.status != 200:
                                return (tag, None)
                            new_response = await new_response.read()
                            return (tag, new_response)
                    tasks.append(fetch(url=f'https://api.clashofclans.com/v1/players/{tag.replace("#", "%23")}', session=session, headers={"Authorization": f"Bearer {keys[0]}"}, tag=tag))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                await session.close()

        for tag, result in results:
            tag_map[tag] = result
        return tag_map

    tag_map = await get_player_responses(keys=deque(api_keys), tags=player_tags)
    return tag_map
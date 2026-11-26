import json
import random
import re

import pandas as pd
from rapidfuzz import fuzz, process

from app import llm
from app.config import CATEGORY_REPORT, PIPELINE

MAPPINGS = PIPELINE / "mappings"
MAP_CSV = MAPPINGS / "category_map.csv"
REVIEW_CSV = MAPPINGS / "review_queue.csv"

# Ordered: the first matching rule wins, so seasonal and specific nouns come before broad ones.
RULES = [
    ("Christmas", r"CHRISTMAS|XMAS|SANTA|ADVENT|REINDEER|NATIVITY|SNOWMAN|ELF |TINSEL|BAUBLE|FATHER CHRISTMAS|STOCKING"),
    ("Candles and Fragrance", r"CANDLE|T-LIGHT|TLIGHT|TEA LIGHT|INCENSE|OIL BURNER|FRAGRANCE|DIFFUSER|SCENTED|WAX"),
    ("Lighting", r"LIGHTS?\b|LAMP|LANTERN|NIGHT ?LIGHT|LED\b|FAIRY|CHANDELIER|LAMPSHADE"),
    ("Bags", r"\bBAGS?\b|SHOPPER|TOTE|PURSE|WALLET|LUNCH ?BOX|BACKPACK|RUCKSACK|HOLDALL"),
    ("Cards and Gift Wrap", r"\bCARDS?\b|WRAP|GIFT TAG|TAGS\b|RIBBON|TISSUE|ENVELOPE|GIFT BOX|PAPER CHAIN"),
    ("Party", r"PARTY|BUNTING|BALLOON|NAPKIN|CAKE CASES?|CAKE STAND|DOILY|DOILIES|BIRTHDAY|CONFETTI|STRAWS|PLACEMAT|PICNIC"),
    ("Kitchen and Dining", r"MUG|CUP|SAUCER|PLATE|BOWL|TEAPOT|TEA ?TOWEL|JUG|BEAKER|GLASS|TUMBLER|CUTLERY|SPOON|KNIFE|FORK|BAKING|CAKE|COOKIE|BISCUIT|KITCHEN|APRON|OVEN|EGG|COASTER|TRAY|COFFEE|TEA |DISH|SCALES|JAM|PANTRY|CHOPSTICK|LADLE|BOTTLE"),
    ("Stationery", r"NOTEBOOK|NOTE ?BOOK|PENCIL|\bPENS?\b|ERASER|RUBBER|STICKER|JOURNAL|DIARY|CALENDAR|STATIONERY|MEMO|SHARPENER|RULER|NOTEPAD|CRAYON|CHALK|ORGANISER|ADDRESS BOOK|BOOKMARK|PAPER"),
    ("Signs and Wall Art", r"\bSIGN\b|SIGNS|PLAQUE|WALL ART|POSTER|PRINT\b|METAL SIGN|DOOR SIGN"),
    ("Frames and Clocks", r"FRAME|CLOCK|PHOTO|ALBUM|MIRROR"),
    ("Jewellery and Accessories", r"NECKLACE|EARRING|BRACELET|\bRING\b|JEWEL|BROOCH|HAIR|CHARM|BEAD|PENDANT|BANGLE|DIAMANTE|SUNGLASSES|SCARF|HAT\b|GLOVES|UMBRELLA|KEY ?RING|BADGE|COMPACT"),
    ("Toys and Games", r"\bTOY|GAME|DOLL|PUZZLE|JIGSAW|SKIPPING|SPACEBOY|DOMINO|PLAYING CARDS|BLOCKS|TEDDY|SOLDIER|MAGIC|KIT\b|COLOURING|SNAKES|LUDO|SKITTLES|SPINNING TOP|YO-YO|MONEY BOX|PIGGY|ROCKET|PAINT SET|NINJA"),
    ("Garden and Outdoor", r"GARDEN|PLANT|BIRD|WATERING|FLOWER POT|POT\b|PLANTER|SEED|PARASOL|WINDMILL|BIRDHOUSE|HEN HOUSE|GNOME|TRUG|SPADE|TROWEL|THERMOMETER|BUTTERFLY"),
    ("Textiles and Soft Furnishings", r"CUSHION|THROW|BLANKET|DOORMAT|DOOR MAT|CURTAIN|QUILT|RUG\b|HOT WATER BOTTLE|PILLOW|FABRIC|FELT|KNITTED|COSY|TEA COSY|BEDSPREAD"),
    ("Bath and Beauty", r"BATH|SOAP|TOWEL|BATHROOM|TOILET|SPONGE|WASHBAG|TOOTH|MAKE ?UP|COSMETIC|LIP|NAIL|PERFUME|BEAUTY|SHOWER|FLANNEL|HANDCREAM|HAND CREAM"),
    ("Storage", r"\bBOX\b|BOXES|TIN\b|TINS|JAR|BASKET|STORAGE|HOLDER|HOOK|RACK|DRAWER|CASE\b|CHEST|CABINET|CRATE|BUCKET|HAMPER|TIDY|PEG"),
    ("Home Decor", r"HEART|DECORATION|HANGING|ORNAMENT|VASE|FEATHER|WREATH|GARLAND|STAR|BELL|DOORSTOP|DOOR STOP|SPIRAL|BUNNY|EASTER|CHICK|ANGEL|DOVE|CHANDELIER|MOBILE|FLOWER|ROSE|WICKER|CERAMIC|SCULPTURE|STAND\b|FIGURE"),
]
CATEGORIES = [c for c, _ in RULES] + ["Other"]
COMPILED = [(c, re.compile(p)) for c, p in RULES]
VOCAB = sorted({w for _, p in RULES for w in re.findall(r"[A-Z][A-Z\-]{4,}", p)})
FUZZY_MIN = 88
REVIEW_BELOW = 0.9


def rule_label(desc):
    for cat, rx in COMPILED:
        if rx.search(desc):
            return cat
    return None


def fuzzy_label(desc):
    best = (None, 0.0)
    for word in re.findall(r"[A-Z]{5,}", desc):
        hit = process.extractOne(word, VOCAB, scorer=fuzz.ratio, score_cutoff=FUZZY_MIN)
        if hit and hit[1] > best[1] and hit[1] < 100:
            label = rule_label(desc.replace(word, hit[0]))
            if label:
                best = (label, hit[1])
    return best


LLM_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "category": {"type": "STRING", "enum": CATEGORIES},
            "confidence": {"type": "STRING", "enum": ["high", "low"]},
        },
        "required": ["id", "category", "confidence"],
    },
}
LLM_SYSTEM = (
    "You label product names from a UK gift retailer with exactly one category from the allowed list. "
    "Use Other only when nothing fits. Answer with JSON only."
)


def llm_labels(descs, batch=80):
    out = {}
    for i in range(0, len(descs), batch):
        chunk = descs[i : i + batch]
        lines = "\n".join(f"{j}: {d}" for j, d in enumerate(chunk))
        prompt = f"Allowed categories: {', '.join(CATEGORIES)}\n\nProducts:\n{lines}"
        res = llm.generate_json(prompt, system=LLM_SYSTEM, schema=LLM_SCHEMA, metered=False)
        if not isinstance(res, list):
            continue
        for item in res:
            try:
                j = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= j < len(chunk) and item.get("category") in CATEGORIES:
                conf = 0.9 if item.get("confidence") == "high" else 0.6
                out[chunk[j]] = (item["category"], conf)
    return out


def load_cache():
    if not MAP_CSV.exists():
        return {}
    cached = pd.read_csv(MAP_CSV, dtype=str)
    cached = cached[cached["source"] == "llm"]
    return {r.description: (r.category, float(r.confidence)) for r in cached.itertuples()}


def categorise(products, use_llm=True):
    """products: DataFrame with stock_code and description (one row per stock code)."""
    rows = []
    for code, desc in zip(products["stock_code"], products["description"]):
        cat = rule_label(desc)
        if cat:
            rows.append((code, desc, cat, "rule", 1.0))
            continue
        cat, score = fuzzy_label(desc)
        if cat:
            rows.append((code, desc, cat, "fuzzy", round(score / 100, 3)))
            continue
        rows.append((code, desc, None, None, None))
    df = pd.DataFrame(rows, columns=["stock_code", "description", "category", "source", "confidence"])

    left = df["category"].isna()
    cache = load_cache()
    todo = sorted({d for d in df.loc[left, "description"] if d not in cache})
    if todo and use_llm and llm.has_key():
        cache.update(llm_labels(todo))
    for i in df.index[left]:
        hit = cache.get(df.at[i, "description"])
        if hit:
            df.loc[i, ["category", "source", "confidence"]] = [hit[0], "llm", hit[1]]
        else:
            df.loc[i, ["category", "source", "confidence"]] = ["Other", "default", 0.0]

    df["confidence"] = df["confidence"].astype(float)
    MAPPINGS.mkdir(parents=True, exist_ok=True)
    df.sort_values("stock_code").to_csv(MAP_CSV, index=False)
    review = df[df["confidence"] < REVIEW_BELOW].sort_values(["confidence", "stock_code"])
    review.to_csv(REVIEW_CSV, index=False)
    return df


def consistency_check(df, n=120, seed=7):
    """Hide rule labels from the LLM and measure agreement. Rules are not ground truth either."""
    ruled = df[df["source"] == "rule"]
    if ruled.empty or not llm.has_key():
        return None
    sample = ruled.sample(min(n, len(ruled)), random_state=seed)
    got = llm_labels(list(sample["description"]))
    pairs = [(r.category, got[r.description][0]) for r in sample.itertuples() if r.description in got]
    if not pairs:
        return None
    agree = sum(a == b for a, b in pairs)
    disagreements = [
        {"description": r.description, "rule": r.category, "llm": got[r.description][0]}
        for r in sample.itertuples()
        if r.description in got and got[r.description][0] != r.category
    ]
    random.Random(seed).shuffle(disagreements)
    return {
        "sampled": len(sample),
        "answered": len(pairs),
        "agreement": round(agree / len(pairs), 4),
        "examples_of_disagreement": disagreements[:10],
    }


def report(df, consistency):
    total = len(df)
    by_source = df["source"].value_counts().to_dict()
    cumulative, stages = 0, []
    for stage in ["rule", "fuzzy", "llm", "default"]:
        n = int(by_source.get(stage, 0))
        cumulative += n
        stages.append({"stage": stage, "products": n, "share": round(n / total, 4), "cumulative": round(cumulative / total, 4)})
    out = {
        "products": total,
        "categories": CATEGORIES,
        "stages": stages,
        "review_queue": int((df["confidence"] < REVIEW_BELOW).sum()),
        "per_category": df["category"].value_counts().to_dict(),
        "llm_consistency_check": consistency,
    }
    CATEGORY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    CATEGORY_REPORT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out

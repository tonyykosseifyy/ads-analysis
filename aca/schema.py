"""The JSON shape Gemini must return for every ad asset.

Categorical fields use fixed lists so hundreds of ads can be grouped and compared;
free-text fields keep the exact detail.
"""

HOOK_TYPES = [
    "result_first", "before_after", "problem_callout", "question", "bold_claim", "curiosity_gap",
    "pov_or_relatable", "tutorial_promise", "social_proof", "expert_or_hairdresser", "reaction",
    "offer_or_price", "trend_or_sound", "product_reveal", "story", "other", "none",
]
FORMATS = [
    "talking_head_ugc", "tutorial_demo", "voiceover_broll", "silent_demo_with_text", "before_after",
    "hairdresser_or_expert", "review_testimonial", "reaction_or_duet", "comparison", "unboxing",
    "grwm_lifestyle", "skit_or_story", "slideshow_or_static", "product_showcase", "other",
]
ANGLES = [
    "salon_result_at_home", "saves_time", "less_heat_damage", "frizz_or_curly_solution", "volume_and_bounce",
    "easy_for_beginners", "replaces_multiple_tools", "price_value_vs_dyson", "quiet_or_gentle",
    "social_proof_popularity", "expert_endorsement", "offer_discount_urgency", "gift_or_occasion",
    "travel_or_on_the_go", "other",
]
PRODUCTION = ["phone_ugc", "edited_ugc", "professional_studio", "brand_footage", "static_design", "mixed"]

_str = {"type": "string"}
_num = {"type": "number"}
_bool = {"type": "boolean"}


def _arr(items):
    return {"type": "array", "items": items}


def _enum(values):
    return {"type": "string", "enum": values}


def _obj(props: dict):
    return {"type": "object", "properties": props, "required": list(props)}


ANALYSIS_SCHEMA = _obj({
    "asset_type": _enum(["video", "image"]),
    "duration_s": _num,
    "aspect_ratio": _str,
    "one_line_summary": _str,
    "full_description": _str,

    "language": _obj({
        "spoken_languages": _arr(_enum(["ar", "en", "fr", "other"])),
        "primary_spoken_language": _enum(["ar", "en", "fr", "mixed", "none"]),
        "arabic_dialect": _str,
        "approx_share_pct": _obj({"ar": _num, "en": _num, "fr": _num}),
        "code_switching": _bool,
        "on_screen_text_languages": _arr(_enum(["ar", "en", "fr", "arabizi", "other"])),
    }),

    "transcript": _arr(_obj({
        "start_s": _num, "end_s": _num, "speaker": _str,
        "language": _enum(["ar", "en", "fr", "mixed"]),
        "text_original": _str, "text_english": _str,
    })),
    "transcript_full_original": _str,
    "transcript_full_english": _str,
    "word_count": _num,
    "speech_starts_at_s": _num,

    "on_screen_text": _arr(_obj({
        "start_s": _num, "end_s": _num, "text": _str,
        "role": _enum(["hook", "caption_of_speech", "benefit", "offer_price", "cta", "brand", "other"]),
    })),

    "scenes": _arr(_obj({
        "start_s": _num, "end_s": _num, "shot_type": _str,
        "visual": _str, "action": _str, "product_visible": _bool,
    })),

    "hook": _obj({
        "first_frame": _str,
        "visual_0_3s": _str,
        "spoken_0_3s": _str,
        "text_0_3s": _str,
        "hook_type": _enum(HOOK_TYPES),
        "hook_type_secondary": _enum(HOOK_TYPES),
        "pattern_interrupt": _str,
    }),

    "structure": _obj({
        "format": _enum(FORMATS),
        "narrative_beats": _arr(_str),
        "main_angle": _enum(ANGLES),
        "secondary_angles": _arr(_enum(ANGLES)),
        "key_claims": _arr(_str),
        "pain_points": _arr(_str),
        "objections_handled": _arr(_str),
        "social_proof": _arr(_str),
        "comparison_to": _str,
    }),

    "product": _obj({
        "products_shown": _arr(_str),
        "first_seen_s": _num,
        "first_used_on_hair_s": _num,
        "final_result_shown_s": _num,
        "demo_clarity": _enum(["clear_step_by_step", "partial", "result_only", "none"]),
        "before_after_shown": _bool,
    }),

    "offer_and_cta": _obj({
        "offer": _str,
        "price_mentioned": _str,
        "discount_code": _str,
        "urgency": _str,
        "cta_spoken": _str,
        "cta_on_screen": _str,
        "cta_at_s": _num,
    }),

    "talent": _obj({
        "people_count": _num,
        "talent_type": _enum(["creator_influencer", "everyday_customer", "hairdresser_expert", "model", "hands_only",
                              "no_person"]),
        "name_if_shown": _str,
        "age_range": _str,
        "hair_before": _str,
        "hair_after": _str,
        "setting": _str,
        "faces_camera": _bool,
        "energy_tone": _str,
    }),

    "audio_and_edit": _obj({
        "audio_type": _enum(["talking", "voiceover", "music_only", "talking_plus_music", "trending_sound",
                             "asmr_product_sound", "silent"]),
        "music_description": _str,
        "captions_burned_in": _bool,
        "production_style": _enum(PRODUCTION),
        "cut_count": _num,
        "avg_shot_length_s": _num,
        "pacing": _enum(["very_fast", "fast", "medium", "slow"]),
        "effects": _arr(_str),
        "ending": _str,
    }),

    "notes": _str,
})

REQUIRED_TOP_LEVEL = list(ANALYSIS_SCHEMA["properties"])

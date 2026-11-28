# Hand-added creatives

For ads whose video Meta won't hand over to the app (posts on a creator's own Instagram/Page, Instagram-only
posts), put the video or image file here, named after the **ad id** or the **media key**:

- `23857964199420795.mp4` (ad id, as in the `ad_id` column of ads.csv)
- `v_3676247799180361.mp4` (media key, as in the `media_key` column of creatives.csv)

The next run (or a `retry-failed` run) analyses it and joins it to that ad's metrics like any other creative.
Accepted: .mp4 .mov .m4v .webm .jpg .jpeg .png .webp

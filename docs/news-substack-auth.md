# Grabbing the right Substack auth cookie

Paywalled Substack articles need a session cookie to fetch in full. This doc walks the operator through getting the right cookie on the first try (rather than the four wrong cookies we tried before getting there).

## TL;DR

The cookie you want is **`connect.sid`**, and it must be copied from **the publication's own domain** (e.g. `www.latent.space`), not from `substack.com`.

Add it to `news.toml` as:

```toml
[sources.<slug>]
type = "..."
sid = "s%3A..."   # connect.sid value from the publication domain
```

## Step-by-step

1. **Log into the publication in your browser.** Visit a paid article and confirm you can read the full body (not the "Keep reading with a 7-day free trial" stub).

2. **Open dev tools.** Chrome / Brave / Edge / Firefox: `Cmd+Opt+I` (macOS) or `F12`. Safari: enable the Develop menu first (Preferences → Advanced → "Show Develop menu"), then `Cmd+Opt+I`.

3. **Navigate to the cookies for the publication's domain.**
   - **Application** tab (Chrome / Edge) or **Storage** tab (Firefox) → expand **Cookies** in the left tree.
   - You'll typically see two entries: `https://substack.com` and `https://<publication>.com` (or whatever the custom domain is, e.g. `https://www.latent.space`).
   - **Click the publication's domain, NOT `substack.com`.** This is the most common mistake — see the "Why not substack.com" section below.

4. **Find the `connect.sid` row.**
   - Value starts with `s%3A` (URL-encoded `s:` — express-session signed-cookie format).
   - The full value is typically 70–100 chars.

5. **Copy the value.** Click the row, then in the detail panel at the bottom (or right side, depending on browser), select the full value and copy.

6. **Paste it into `news.toml`:**
   ```toml
   sid = "s%3A...the value you copied..."
   ```

7. **Test:**
   ```bash
   ./news.sh --source <slug> --check
   ./news.sh --source <slug> --limit 1
   ```
   Inspect the resulting `.md` file — if the body extends well past where the paywall stub used to be (no "Keep reading with a 7-day free trial" line), auth worked.

## Why not `substack.com`?

Custom-domain Substack publications (`www.latent.space`, `blog.cloudflare.com`, etc.) are a **separate origin** from `substack.com`. The browser isolates cookies between origins, so cookies set on `.substack.com` never reach `www.latent.space`.

When you log in, Substack actually sets cookies on **both** domains (via an OAuth-style cross-domain handshake — a hidden iframe loads `substack.com/channel-frame` and posts the auth back to the parent page). But the **paywall enforcement is on the publication domain**, and it checks the cookie set there.

If you copy from `substack.com`, the cookies you'll see (`substack.sid`, `substack.lli`, etc.) are real auth cookies — they just don't matter for the article fetch. The wrong cookie, sent to the wrong origin, gives a 200-OK response with the public preview.

## What about the other Substack cookies?

On `substack.com` you'll see `substack.sid`, `substack.lli` (a JWT), plus various tracking cookies. They're real, but irrelevant to per-article paywall checks:

- **`substack.sid`** — express-session cookie for `substack.com` itself (admin pages, settings).
- **`substack.lli`** — JWT with `aud: "likely-logged-in"`. It's a UX hint for "show username in nav," not an authz token.
- **`__cf_bm`, `cf_clearance`** — Cloudflare bot-management cookies. Required for some sites, but Substack's own infra usually doesn't need them on the article API.

For paid bodies, all you need is **`connect.sid`** on the publication domain.

## Verifying the cookie works (before adding to `news.toml`)

If you want to confirm the value before plumbing it through:

```bash
export SID="s%3A...your-cookie-value..."
curl -sL "https://www.latent.space/p/<some-paid-slug>" \
    -H "Cookie: connect.sid=$SID" \
    -o /tmp/article.html

# Check whether the paywall stub is present
grep -c "Keep reading with" /tmp/article.html
# 0 = unlocked. 1+ = still gated (wrong cookie).
```

Same check via the JSON API (returns more structured data; also gated):

```bash
curl -sL "https://www.latent.space/api/v1/posts/<slug>?referrer=" \
    -H "Cookie: connect.sid=$SID" \
    | jq '.audience, (.body_html | length)'
# audience = "only_paid" + body_html > 30000 chars = unlocked
```

## Rotation and expiry

`connect.sid` is a session cookie. Express-session defaults to one of:

- A fixed Max-Age (typically 14–30 days — Substack uses ~30 days for paid users).
- Re-issued on activity (sliding expiration) — visiting the site refreshes it.

In practice: as long as you log into the site at least once a month from the same browser, the cookie keeps rotating without expiring. When `news.sh` starts getting paywall-stub bodies back, that means the cookie has rolled over — just grab a fresh one and update `news.toml`.

No code needs to change on rotation. The same field, same format, same flow.

## Adding a new Substack source

Common pattern for adding any Substack publication:

1. Probe the publication for an RSS feed:
   ```bash
   curl -sL "https://<publication>.com/feed" | head -20
   ```
2. If RSS exists and the content you want is paid, grab `connect.sid` per above.
3. Drop a block in `news.toml`:
   ```toml
   [sources.<slug>]
   type = "rss"
   url = "https://<publication>.com/feed"
   sid = "s%3A..."
   ```
4. Optionally — if you want the full history beyond RSS's 20-entry cap, switch to the archive API instead:
   ```toml
   [sources.<slug>]
   type = "substack-section-archive"
   base_url = "https://<publication>.com"
   section_id = <numeric id from /s/<section> page's <link rel="alternate">>
   sid = "s%3A..."
   min_date = "2026-01-01"   # optional, recent-floor
   ```

## Related code

- `news.py:fetch_and_extract` — the function that applies the cookie.
- `news.py:discover_substack_archive` — the paginated archive walker.
- `news.example.toml` — example config blocks for both `type = "rss"` and `type = "substack-section-archive"`.

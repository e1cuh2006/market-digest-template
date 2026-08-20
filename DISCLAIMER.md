# Disclaimer

Please read this before running or sharing this project.

---

## Not financial advice

This tool reports **prices and headlines** and does arithmetic on them. That is
all it does.

- It does **not** recommend buying, selling, or holding anything.
- It does **not** predict prices or forecast returns.
- The day and week recaps are **descriptive statistics** — counts, averages,
  best and worst performers — not analysis, opinion, or a signal.
- Nobody involved in this project is a licensed financial advisor, and nothing
  here is personalized investment advice.

Do your own research, and consult a qualified professional before making
investment decisions.

## Data accuracy

Every data source is a **free tier**, with the limitations that implies:

- Prices are quoted against the **previous close**, not streamed in real time.
  They are correct for a twice-daily digest and wrong for intraday trading.
- Free tiers can be delayed, rate-limited, or briefly unavailable. When a source
  fails, the digest still sends — that ticker simply shows less detail.
- Coverage has gaps. Some ETFs lack logos or news; non-US listings may not
  resolve at all.
- Per-ticker news is matched by **keyword**, so an occasional irrelevant
  headline slips through. Ticker symbols also get reused and reassigned over
  time — verify a symbol is what you think it is.

Never make a decision on this digest alone. Confirm anything that matters
against your broker or an authoritative source.

## Security and your credentials

This project needs an API key and a **Gmail App Password** to work. Handle them
carefully:

- **Never commit credentials.** Put them in `.env` (already gitignored) or in
  GitHub Actions **Secrets** — never in the code, and never in `watchlist.json`.
- **Never paste your App Password into an AI chat** — Claude, ChatGPT, Copilot,
  or anything else. It is fine to ask an AI assistant to help you customize
  tickers, layout, or the schedule; it is **not** fine to hand it a password or
  key. Type those directly into `.env` or the GitHub Secrets page.
- **Use an App Password, not your Google password.** It can only send mail, and
  you can revoke it at
  [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
  at any time without affecting your account.
- **Keep your repo private**, and only grant collaborators **Read** access.
  Someone with **Write** access to a repo could modify the workflow to extract
  its secrets — so treat write access as equivalent to handing over the
  credentials themselves.
- **Your secrets stay yours.** Repository secrets are encrypted and cannot be
  read back by anyone — not collaborators, and not you. Cloning or forking a
  repo never copies its secrets. If you shared this project with someone, they
  received the *code*, never your credentials.

If you suspect a credential leaked, revoke it immediately: delete the Gmail App
Password, and regenerate the Finnhub key from its dashboard.

## Customizing with an AI assistant

This repo is documented well enough that an AI coding assistant can help you
modify it. Safe things to ask for:

- Adding or removing tickers
- Changing send times or how many headlines appear
- Adjusting the email layout, colors, or sections

Just keep credentials out of the conversation, per the rule above.

## No warranty

This is a personal project provided **as is**, without warranty of any kind.
Nobody involved is liable for missed emails, incorrect data, financial losses,
or any other damages arising from its use. Third-party services (Finnhub,
CoinGecko, Yahoo, Gmail, GitHub Actions) may change, rate-limit, or discontinue
access at any time, and each is governed by its own terms of service — make
sure your usage complies with them.

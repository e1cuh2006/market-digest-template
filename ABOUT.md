# About this project

*What this is, why it exists, and the thinking behind how it works.*

---

## The problem

Following a handful of positions across stocks and crypto is annoyingly
high-friction. The information is scattered — a brokerage app for equities, a
different app or site for crypto, and a news feed somewhere else — and none of
them talk to each other. Checking on ten tickers means opening four apps and
assembling the picture yourself, every single time.

The bigger issue is that those apps are not designed to inform you. They are
designed to keep you in them. Price alerts, red and green flashes, push
notifications on every 2% move, an infinite feed of headlines ranked by how
likely they are to make you tap. The incentive is engagement, and for someone
holding long-term positions, engagement is mostly noise. Checking a position
fifteen times a day does not produce a better decision than checking it twice.

## What this does instead

It sends one email, twice a weekday — at **9:30am ET** when the U.S. market
opens, and at **5:00pm ET** after it closes. Each one contains:

- **Every ticker on the watchlist** with its current price, percent change, and
  a small chart of recent price action
- **Recent headlines** for each individual ticker
- **A day recap and a week recap** — breadth, leaders and laggards, how the
  group sits against 52-week ranges, and how far crypto is from record highs
- **Macro headlines** covering the Fed, inflation, rates, tariffs, and other
  events that move markets broadly

Then it stops. There are no push notifications, no alerts, no feed to scroll.
The information arrives at the two moments in the day when it is actually
actionable, and stays out of the way the rest of the time.

That cadence is the entire point. This is a **pull-shaped tool wearing a push
envelope**: it comes to you, but only at times you chose in advance, and it can
never interrupt you more often than that. It is deliberately impossible to
refresh.

## Design decisions, and why

**Watchlist, not portfolio.** The tool tracks tickers, not share counts, and
computes no dollar totals or profit-and-loss. This was a deliberate choice. A
running P&L figure invites emotional reactions to numbers that only matter when
you actually transact. Tracking price action and news, without a live scoreboard
of personal gains and losses, keeps the focus on what is happening in the market
rather than how it feels.

**Email as the interface.** Email is universal, searchable, and archived by
default. There is no app to install, no account to create, and no service that
can shut down and take the tool with it. Six months of digests sitting in a
Gmail folder is a genuinely useful record — searchable by ticker, by date, by
headline.

**Zero dependencies.** The entire program is Python standard library. No pip
packages, no lockfile, no supply chain to audit, nothing that breaks when an
upstream library ships a major version. This includes the charts: the sparkline
PNGs are encoded by hand with `zlib` and `struct` rather than pulling in a
plotting library. It means the project will still run unchanged years from now.

**Free by design.** Every data source is a free tier: Finnhub for stock quotes
and news, CoinGecko for crypto, Yahoo for intraday and 52-week data, and GitHub
Actions for scheduling. Running cost is zero. An earlier version generated
written market commentary through a paid AI API; it was removed in favor of
recaps computed directly from the price data, because keeping the tool free and
deterministic was worth more than prettier prose.

**Previous-close pricing.** Prices are quoted against the previous close rather
than streamed in real time. For a digest that arrives twice a day, tick-level
accuracy is irrelevant — and chasing it would mean paid data feeds. The daily
close is the number that matters at this cadence, and it is exact.

**Cloud-scheduled.** It runs on GitHub Actions rather than a local cron job, so
the email arrives whether or not a laptop is open. The schedule fires at both
the EDT and EST clock times and the script sends only inside the correct Eastern
window, so it stays accurate across daylight saving with no maintenance.

## What it is not

- **Not investment advice.** Every digest states this explicitly. The tool
  reports prices and headlines and computes arithmetic on them. It does not
  recommend buying or selling anything, and it does not attempt to predict
  prices.
- **Not a trading tool.** Twice-daily, previous-close data is the wrong
  instrument for intraday trading, by design.
- **Not comprehensive.** Free data tiers have gaps — some ETFs lack logos or
  news coverage, and per-ticker headlines are matched by keyword, so an
  occasional irrelevant story slips through.

## Why it matters

The useful thing here is not the code, which is a few hundred lines. It is
**owning the pipeline**. The tickers, the schedule, the layout, the sources, and
what counts as important are all defined in files you control, rather than by a
company whose revenue depends on your attention. Adding a ticker is one line in
a JSON file. Changing the send time is one line in a YAML file. Nothing is
negotiated with a product team optimizing for a different goal than yours.

It is a small, boring, durable piece of infrastructure that does exactly one
thing on a schedule and then leaves you alone — which, for following a
watchlist over months and years, turns out to be the right shape for the job.

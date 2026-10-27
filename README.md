# Telegram Moderation Guardrail Bot

A Python Telegram bot prototype that watches for bursts of high-impact moderation actions, such as bans or member removals. The idea is to help group admins notice when another bot may be acting too broadly, including after a bug or a misconfiguration.

## Project goals

- Observe relevant member status changes in a Telegram group.
- Track removals over a configurable time window.
- Apply thresholds and cooldowns to reduce repeated or overbroad responses.
- Keep moderation decisions reviewable by human admins.

## Built with

Python · python-telegram-bot · Telegram Bot API · Event Monitoring · Automation

## Security before running

Use a newly issued bot token stored in an environment variable. Never publish a token or state file. Give the bot only the Telegram permissions it needs and test it in a private group before relying on it. This is a prototype, not a guarantee against moderation mistakes.
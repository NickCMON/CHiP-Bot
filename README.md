# CHiP Bot

CHiP is CMON's Discord translation and role bot. This build replaces the
unofficial Google translation endpoint that occasionally posted an HTML
`Error 500` page into Discord.

## Translation setup

- Spanish source messages use `gpt-5.6-terra` once to understand slang, jokes,
  sarcasm, and gaming tone, then Azure translates the clarified English into
  the other languages.
- All other source languages use Azure Translator directly.
- If Terra is paused, reaches CHiP's local cap, or has an API error, Spanish
  automatically falls back to Azure.
- If Azure has an API error, CHiP relays the original human message and records
  the error privately. Provider error pages are never posted into chat.
- Media-only messages never call either paid translation service.

Official setup references:

- [Azure Translator REST quickstart](https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/quickstart/rest-api)
- [GPT-5.6 Terra model](https://developers.openai.com/api/docs/models/gpt-5.6-terra)
- [OpenAI usage dashboard](https://platform.openai.com/usage)

## Railway variables

Copy every variable from `.env.example` into the Railway service's **Variables**
page. Never commit real keys or the Discord token to GitHub.

Important values:

```text
ANNOUNCEMENT_CHANNEL_ID=1529511999891964015
AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
OPENAI_MODEL=gpt-5.6-terra
TERRA_BUDGET_USD=4.50
CHIP_DATA_DIR=/data
```

For Azure, create a **Translator**, **single-service**, **global**, **F0**
resource. Copy its key into `AZURE_TRANSLATOR_KEY`. A global resource can leave
`AZURE_TRANSLATOR_REGION` blank. A regional or multi-service resource must use
the exact region shown on Azure's **Keys and Endpoint** page.

For Terra, create a dedicated OpenAI project named `CHiP`, generate a project
API key, add the initial prepaid credit, and turn automatic recharge **OFF**.
`/terra limit` changes only CHiP's local safety cap; it cannot add credit or
charge a card.

## Persistent usage tracking on Railway

Add a Railway volume and mount it at `/data`, then set:

```text
CHIP_DATA_DIR=/data
```

Without a volume, Railway can erase the SQLite usage database during a new
deployment. With the volume, message links, maintenance state, Terra usage, and
the local budget setting survive restarts and deployments.

## Admin slash commands

The commands sync to the Discord server containing the configured CHiP
announcement channel.

- `/maintenance start [reason]` — posts in the existing CHiP announcement
  channel, sends localized notices to every translation channel, changes
  CHiP's status, and pauses translation relays.
- `/maintenance end` — resumes relays, restores CHiP's status, and announces
  that maintenance is complete.
- `/maintenance status` — shows whether maintenance is active and when it began.
- `/terra status` — shows tracked total and month-to-date cost, remaining local
  budget, requests, Spanish messages, and token usage.
- `/terra limit <amount_usd>` — changes CHiP's **cumulative** tracked spending
  cap. If $4.50 has been used and another $5 is added, set the new cap to $9.50.
- `/terra pause` — switches Spanish to Azure-only.
- `/terra resume` — resumes Terra if a key and remaining local budget exist.

All of these slash commands require the Discord **Administrator** permission.

## Emoji, sticker, GIF, and attachment relay

- Unicode emojis remain unchanged.
- Static and animated custom Discord emoji markup is protected from translation.
- GIF and other web links are protected from translation.
- Uploaded images, GIFs, and attachments are downloaded once and re-uploaded to
  each language channel when they fit the configured size limit.
- PNG, APNG, and GIF stickers are relayed as visible uploads. Lottie stickers
  use their protected Discord CDN link because they cannot be re-uploaded as a
  normal visible image.
- If a media upload fails, CHiP retries with the original protected CDN link.

CHiP needs these permissions in every translation and announcement channel:

- View Channel
- Send Messages
- Read Message History
- Add Reactions
- Manage Webhooks
- Attach Files
- Embed Links

## Deploy and test

1. Commit these files to the existing GitHub repository.
2. Add the Railway variables and `/data` volume.
3. Deploy or restart the Railway service.
4. Run `/terra status` and `/maintenance status` in Discord.
5. Test a Spanish slang message, a custom animated emoji, a sticker, a GIF link,
   and an uploaded image in a translation channel.
6. Check Railway logs for `Azure: True` and `Terra: True` on startup.

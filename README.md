# CoronetBot

LLM-based moderation bot for the Coronet strata community server.

## Behaviour

Once the bot is installed on the server it follows the following rules:

For every new message/post/reply in every channel, thread or forum, the bot should do the following:

1. Read the message
2. Analyse it against a set of rules
3. If the message doesn't break rules, do nothing
4. If the message breaks any rules:
   1. DM the user in the specified format (see below)
   2. Delete the original message
   
## DM format

Your message in #general was removed and has not been retained publicly.

```
Original draft:
> [complete original message]

Reasons:
• Personal attack: “dishonest idiot”
  Criticises the person rather than their conduct.

• Unsupported accusation: “stole the money”
  Presents alleged wrongdoing as an established fact.

Suggested revision:
“The reported figures do not appear to match the payment records.
Could someone review and clarify the discrepancy?”

You can copy and revise your original draft above. You may use /validate here to validate/refine your message before trying to send it again - that should help you avoid triggering slow-mode only for your message to get deleted seconds later. Use /rules to see the set of rules used for moderation.
```

## DM commands

/validate(text) - allows the user to validate their message before trying to post it
/rules - prints the rules used for validation
/help - list commands, version, llm model and configuration being used


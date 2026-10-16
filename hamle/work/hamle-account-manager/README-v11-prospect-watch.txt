V11 prospect watch: one warm first reply to unknown senders (owner 2026-10-15)
==============================================================================
v11_prospect_watch.py answers mail sent to the agency mailbox by senders who are
neither agency staff nor known client contacts. The public site's button opens a
mail with the subject "Hamle AI demo talebi". It is a new Cloud Run job:

  hamle-prospect-watch   worker.py --mode prospect_watch
  schedule               every 15 minutes, every day, 08:00-22:00 Istanbul
                         (scheduler-plan-prospect-watch.json: "*/15 8-21 * * *"
                         and "0 22 * * *"); the client chain hours are unchanged.

The job is OFF until prospect-watch-config.json has enabled=true. It also needs
the image rebuilt (package_chain_source.py), the job and schedulers created
(not done: local plan only), and the two Basecamp lists in place. Nothing in
code names a person: the owner and the new business lead are roles in config,
resolved to verified Basecamp members from the catalog.

Per mail
--------
1. Pre-filter in code, no model call, counted in the job report only (no
   durable row): Auto-Submitted other than "no", X-Autoreply, X-Autorespond,
   Precedence bulk/list/junk, List-Unsubscribe, List-Id, noreply / no-reply /
   mailer-daemon / postmaster / config ignore_locals senders, config
   ignore_domains, and Authentication-Results. Only our provider's own line
   (auth_serv_id) counts; any SPF/DKIM/DMARC fail skips the mail; a missing line
   or no aligned pass also skips. Mail older than config 'since' is skipped.
2. Durable claim per message (one handling, fenced by the global control).
   At most ONE automatic reply per address, ever: a later mail from the same
   address becomes a comment on its first note, with no model call. At most
   limits.max_auto_replies_per_day automatic replies per Istanbul day; past
   that, the team gets a "human should look" note.
3. Classification on the Haiku route (prospect_classifier), schema-checked:
   demo_or_first_contact, research_request, known_client_new_person,
   vendor_or_job, spam, unsure. Mail text is untrusted data.
4. The company comes ONLY from the sender's domain:
   - free mail: an individual; no company research; the reply asks which
     company they write from;
   - a config client_domains domain: a warm reply that knows that brand, and a
     note to the brand's lead (rep_person_id when a member, else the new
     business lead) asking "Bu kişi kim, ekleyelim mi?";
   - any other domain: one Opus web-search call (prospect_research, at most 2
     searches: the company, and the person only when not on the owner's
     known-people list; professional sources only). The free digital-karne
     site checks follow, with no model inside them.
   What a sender claims about their own company is noted, never used.
5. Person findings (a title only on a clear, sourced, non-social match) go only
   into the internal note. The reply model never sees them.
6. The reply (customer_text, Opus) sees only: today, language, category,
   domain kind, first name, domain-derived company facts, the known brand name
   (client domain only), the known person's "where we met", whether to add the
   research sentence, the subject and the new mail text. No brand memory, no
   client data, no internal notes. Code checks the reply for: greeting with
   first name, exact signature, no Hanım/Bey, no prices, dates or promises, no
   links, addresses or phone numbers, no research words, no internal terms, no
   other brand names, the owner named for the meeting, the approved research
   sentence exactly when required, and length. A failed check sends nothing and
   opens the note for a human.
7. The reply goes in the same thread, only to the sender (no CC), after a
   preflight (still the newest incoming mail, recipients and subject unchanged).
   There is one POST and a readback; an uncertain send is never retried. A
   separate copy mail goes to the owner. A to-do opens in the notes list,
   assigned to the new business lead and the owner (or the brand lead and the
   owner). It holds the sender, company, class, the ask, findings, the reply
   and the quoted original. vendor_or_job and spam are logged only (vendor rows
   carry a digest_line). unsure gets no reply and a note.

Research requests (organisations only)
--------------------------------------
- The first reply adds exactly the approved sentence.
- A to-do opens in the research list (found by name), assigned to the owner,
  due that day, with the estimate (config research.estimate).
- There is one open request per company domain.
- Nothing is researched (no model, no web) until the owner's verified Basecamp
  person writes exactly "Onaylıyorum" on the to-do. Case, spaces and a final
  "." or "!" are ignored, and İ/ı are folded. Another person's approval does
  nothing.
- After approval, one prospect_report call (Opus, 8 searches) is posted as a
  comment. It covers sourced observations, recent campaigns and "biz olsak ilk
  3 hamlemiz", plus the karne checks and a mail draft. Unsourced items are
  dropped.
- The report reaches the prospect (same thread, only the requester) only after
  the owner writes "Hamle gönder" (an optional corrected text below it).
  "Hamle gönderme" rejects it.
- One reminder a day after research.reminder_after.
- A request expires after research.expire_days.
- "Hamle dur" from any agency member on the to-do stops that request.
- Requests to research a PERSON are refused (noted, no request).

Safety
------
- Global stop ('dur'), takeover or a new control generation stops the run
  before any read, and fences every write.
- The monthly budget (cloud_store) and the internal model cap gate every model
  call.
- ModelGate gives each input exactly one durable chance.
- A missing or client-visible list holds the run before any reply.
- A missing client contact table holds the run, because the job cannot tell
  clients apart.
- A thread where agency staff already wrote, or where the mailbox is only in
  CC, gets a note and no automatic reply.
- Teams: the Heads notice is NOT built. The Teams adapter only replies to an
  incoming activity and has no proactive messaging (no stored serviceUrl or
  conversation reference).

KVKK
----
- Every record is listed in a monthly index.
- After retention_days (365) a daily purge in the run replaces each record
  with a data-free tombstone.
- The address marker keeps only "replied", so the one-reply rule survives the
  purge.
- Filtered mail leaves no record.

Owner settings (prospect-watch-config.json)
-------------------------------------------
- enabled
- roles.owner, roles.new_business_lead
- basecamp.project_id, notes_list, research_list
- limits: max_auto_replies_per_day 10, max_mails_per_run, run_seconds,
  reply_max_chars
- window
- signatures (tr/en)
- approved_sentences.research_request
- research: enabled, estimate, reminder_after, expire_days
- karne_checks
- client_domains
- ignore_domains / ignore_locals / free_mail_domains
- retention_days

Known people (name, company, title, where_met, note, emails/domains) live in
Firestore, never in git:
  python3 v11_prospect_watch.py people import --file known-people.json --actor <owner address>
  python3 v11_prospect_watch.py people status

Cost per new prospect mail (estimate, Anthropic list prices)
------------------------------------------------------------
- Haiku classification: about 0.001 USD.
- Opus research with at most 2 searches: about 0.10-0.15 USD.
- Opus reply: about 0.05-0.08 USD.
- Total: about 0.15-0.25 USD per mail.
- Individuals skip the research call.
- An owner-approved report: about 0.5-1.5 USD.
- An empty run makes 0 model calls.

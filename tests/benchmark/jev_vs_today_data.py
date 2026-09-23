"""Synthetic fixtures with authored ground truth for the Jev comparison.

NAMES ARE PLACEHOLDERS. Some of these sentences were modelled on the SHAPE
of real meeting text, so every person, client, matter and company name here
is invented. This repository is public; nothing identifying a real party
belongs in it, and the linguistic shape is the only thing the fixture needs.

Every item is written from the SS manifest's own definitions, not from what
either scorer happens to say. Balanced by construction, and deliberately
multilingual, because CQ ingests Spanish and Portuguese meetings and the
shipped character guardrail is an English denylist.
"""

# TASK 1. Conduct vs character (guardrail 12b).
# keep=True  -> conduct, a moment a reader could have watched.
# keep=False -> character, a verdict about who the person is. Must be dropped.
CHARACTER = [
    ("Asked for the cost breakdown before agreeing to the vendor switch", "Denby", True, "en"),
    ("Pushed the launch date out after seeing the churn numbers", "Fenwick", True, "en"),
    ("Read the compliance clause aloud instead of summarizing it", "Lockridge", True, "en"),
    ("Declined to approve the budget until the headcount plan was attached", "Okafor", True, "en"),
    ("Pidio el desglose de costes antes de aprobar el cambio de proveedor", "Herrera", True, "es"),
    ("Rechazo firmar el contrato hasta revisar la clausula de rescision", "Herrera", True, "es"),
    ("Adiou a decisao sobre o orcamento ate receber os numeros de rotatividade", "Moreira", True, "pt"),
    ("Leu a clausula em voz alta em vez de resumi la", "Moreira", True, "pt"),
    ("Is defensive about code review feedback", "Denby", False, "en"),
    ("Is a detail oriented and thorough engineer", "Fenwick", False, "en"),
    ("Tends to be dismissive of junior developers", "Lockridge", False, "en"),
    ("Is impatient when meetings run long", "Okafor", False, "en"),
    ("Es defensivo cuando recibe criticas sobre su codigo", "Herrera", False, "es"),
    ("Es una persona muy detallista y meticulosa", "Herrera", False, "es"),
    ("E impaciente quando as reunioes se prolongam", "Moreira", False, "pt"),
    ("Tende a ser desdenhoso com os desenvolvedores juniores", "Moreira", False, "pt"),
]

# TASK 2. Dedup gray zone: same underlying fact or two facts.
# Written to sit where trigram similarity cannot decide: paraphrases that
# share few words, and near misses that share many.
DEDUP = [
    ("Deploy the billing API by end of week", "Ship the billing API before Friday", True),
    ("Send the revised pricing deck to Northwind", "Get the updated pricing deck over to Northwind", True),
    ("Waiting on the court to send the temporary order", "Blocked until the court issues the temporary order", True),
    ("Close the Halloway case today", "Wrap up and close out Halloway this afternoon", True),
    ("Enviar a proposta revisada ate sexta", "Send the revised proposal by Friday", True),
    ("Prefers handwritten notes over digital artifacts", "Would rather take notes by hand than type them", True),
    ("Call the district clerk about the FedEx labels", "Follow up by phone with the district clerk on the FedEx labels", True),
    ("Migrate the legacy spreadsheets into Vaultline", "Move the old spreadsheet data over to Vaultline", True),
    ("Deploy the billing API by end of week", "Deploy the reporting API by end of week", False),
    ("Go live on the 16th", "Go live on the 18th", False),
    ("Prefers on premises models", "Prefers hardware level environment segregation", False),
    ("Zero data retention with AI providers", "Anonymize everything sent to an LLM", False),
    ("Review the petition for the Alder case", "Draft the final order for the Alder case", False),
    ("Schedule a call with the Brightwater client", "Schedule a call with the Marlowe client", False),
    ("Teams collection is capped at 10000 files per minute", "Google Workspace hides files from viewer level accounts", False),
    ("Enviar a proposta revisada ate sexta", "Enviar o contrato assinado ate sexta", False),
]

# TASK 3. Which type is this actually? The drift the classifier exists to catch.
# Ground truth from the manifest definitions the classifier is shown.
TYPES = [
    ("Asked for the cost breakdown before agreeing to the vendor switch", "Denby", "moment"),
    ("Moved off the launch date once the churn numbers were on screen", "Fenwick", "moment"),
    ("Answered the compliance question by reading out the specific clause", "Lockridge", "moment"),
    ("Declined to sign off until the headcount plan was attached", "Okafor", "moment"),
    ("Redirected the discussion from chatbot safety to production reliability", "Ryan", "moment"),
    ("Will send the revised deck on Thursday", "Denby", "commitment"),
    ("Draft the final order for the Alder case by Monday", "Okafor", "commitment"),
    ("Call the district clerk to follow up on the FedEx labels", "Mandy", "commitment"),
    ("The vendor migration slipped again", "Fenwick", "event"),
    ("Court hearing rescheduled to September 30th at 9:30 AM", "Mandy", "event"),
    ("Looked up the account balance when asked", "Okafor", "event"),
    ("Relayed that the answer was filed this morning", "Mandy", "event"),
    ("Vaultline solves the foldering limits present in Google Vault", "Okafor", "takeaway"),
    ("Teams data collection is capped by the Purview API", "Okafor", "takeaway"),
    ("The bigger production risk is the agent acting wrongly, not speaking wrongly", "Ryan", "takeaway"),
    ("Prefers to deliver hard news directly, backed by evidence", "Denby", "preference"),
    ("Would rather start with a single agent and decompose only when justified", "Ryan", "preference"),
    ("Pricing is fixed at 982.80 per month for every channel", "Denby", "decision"),
    ("The team will file an amended motion when the client does not sign", "Mandy", "decision"),
    ("Vaultline will present the deployment pitch to the advisory team", "Denby", "decision"),
]

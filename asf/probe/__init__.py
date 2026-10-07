"""asf.probe — the production verification probe (F-0051): after every deploy, ASF dispatches
the product's own headless journey workflow, signs in through a code mailed to a configured
identity, walks the customer-facing paths that release carried, and reads back one verdict per
journey.

ASF drives no browser and ships no journey engine: the engine is the product's, in the product's
repo, on the product's runners, behind one workflow contract and one result schema. This package
chooses the journeys, dispatches, reads back, judges, records and files — never a page, a
selector or a click.

A namespace only: nothing is imported here, and every module under it is imported by its own
name.
"""

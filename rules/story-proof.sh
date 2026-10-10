#!/usr/bin/env bash
# F-0040 — every landed Task ticks the acceptance line its tests prove.
exec asf proves check --product "${ASF_PRODUCT:?}"

#!/usr/bin/env bash
# A brief naming an existing spec or plan refines it, never regenerates it (F-0023).
exec asf refine-check --product "${ASF_PRODUCT:?}"

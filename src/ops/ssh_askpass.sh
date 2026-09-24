#!/bin/sh
printf '%s\n' "${SSH_ASKPASS_PASSWORD-${VLA_SSH_ASKPASS_PASSWORD-}}"

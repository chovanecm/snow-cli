"""HTTP timeouts as (connect, read) seconds.

The read timeout limits the wait for each chunk of data, not the whole call.
"""

CONNECT_TIMEOUT = 10
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, 60)
# /sys.scripts.do sends nothing until the script ends and scripts legitimately
# run for minutes, so only connecting is limited.
SCRIPT_TIMEOUT = (CONNECT_TIMEOUT, None)

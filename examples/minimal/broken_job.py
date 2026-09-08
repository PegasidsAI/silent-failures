"""A job that does nothing, and says it went fine.

This is not a strawman. It is what a real job looks like once its input has
gone quiet: it runs, it handles the empty case gracefully, it exits zero, and
it produces nothing.
"""
rows = []                       # the upstream source returned nothing
if not rows:
    pass                        # nothing to write; do not clobber good data

print("report written")         # the log line still appears

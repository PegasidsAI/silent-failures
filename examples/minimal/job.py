"""A job that does something: it appends a row to a report."""

import datetime
import os

os.makedirs("out", exist_ok=True)
path = "out/report.csv"
first = not os.path.exists(path) or os.path.getsize(path) == 0

with open(path, "a", encoding="utf-8", newline="\n") as f:
    if first:
        f.write("date,rows\n")
    f.write("%s,%d\n" % (datetime.date.today().isoformat(), 42))

print("report written")

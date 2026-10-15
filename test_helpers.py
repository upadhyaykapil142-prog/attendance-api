from datetime import datetime
from app.main import compute_late_minutes, compute_work_hours, compute_overtime, IST

print("--- LATE (shift 09:30) ---")
for hh, mm, ss, expected in [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45)]:
    t = datetime(2026, 7, 6, hh, mm, ss, tzinfo=IST)
    print("late", hh, mm, ss, "->", compute_late_minutes(t, "09:30"), "expected", expected)

print("--- WORK HOURS ---")
start = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
end = datetime(2026, 7, 6, 11, 40, 30, tzinfo=IST)
print("hours", compute_work_hours(start, end), "expected 2.68")

print("--- OVERTIME (shift end 18:30) ---")
for hh, mm, ss, expected in [(18, 45, 0, 0), (19, 0, 0, 30), (19, 10, 30, 40)]:
    t_out = datetime(2026, 7, 6, hh, mm, ss, tzinfo=IST)
    print("ot", hh, mm, ss, "->", compute_overtime(t_out, "18:30", "2026-07-06"), "expected", expected)
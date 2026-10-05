import re

# ── Fix cli.py _one_geocode ───────────────────────────────────────────
with open("src/or_engine/cli.py", "r", encoding="utf-8") as f:
    content = f.read()

# Replace the cache section in _one_geocode
old = '''     # check cache first
    if cache is not None:
        hit = await cache.get_geocode(addr, city)
        if hit is not None:
            print("             [cache]     ->  HIT   " + repr(addr) + "  city=" + repr(city) + "   (" + str(hit.location.lat) + ", " + str(hit.location.lng) + ")")
            return hit

    print(f"             [amap]       ->  geocode/geo  address={addr!r}  city={city!r}")'''

new = '''     # check cache first
    if cache is not None:
        hit = await cache.get_geocode(addr, city)
        if hit is not None:
            print(f"             [cache]  HIT   geocode/geo  {addr!r}  "
                  f"city={city!r}  -> ({hit.location.lat}, {hit.location.lng})")
            return hit
        print(f"             [cache]  MISS  geocode/geo  {addr!r}  "
              f"city={city!r}  (will call AMap)")

    print(f"             [amap]       ->  geocode/geo  address={addr!r}  "
          f"city={city!r}")'''

if old in content:
    content = content.replace(old, new, 1)
    print("OK: cli.py cache hit print reformed")
else:
    print("WARN: _one_geocode cache-hit print pattern not found exactly, trying alt...")
    # Try finding and replacing just the hit print line
    alt_old = '            print("             [cache]     ->  HIT   " + repr(addr) + "  city=" + repr(city) + "   (" + str(hit.location.lat) + ", " + str(hit.location.lng) + ")")'
    alt_new = '''        print(f"             [cache]  HIT   geocode/geo  {addr!r}  "
                  f"city={city!r}  -> ({hit.location.lat}, {hit.location.lng})")'''
    if alt_old in content:
        content = content.replace(alt_old, alt_new, 1)
        print("OK: reformatted cache HIT print (alt)")
        # Add miss print after the if block
        content = content.replace(
            "            return hit\n",
            "            return hit\n"
            "        print(f\"             [cache]  MISS  geocode/geo  {addr!r}  "
            "f\"city={city!r}  (will call AMap)\")",
            1,
        )
        print("OK: added cache MISS print")
    else:
        print("SKIP: could not find cache hit print in _one_geocode")

# Add cache INSERT print after save
content = content.replace(
    "         if cache is not None:\n"
    "            await cache.save_geocode(addr, city, g.name or \"\",\n"
    "                                     g.location.lat, g.location.lng)\n"
    "         return g",
    "         if cache is not None:\n"
    "            await cache.save_geocode(addr, city, g.name or \"\",\n"
    "                                     g.location.lat, g.location.lng)\n"
    "            print(f\"             [cache]  INSERT geocode/geo  {addr!r}  "
    "f\"city={city!r}  -> ({g.location.lat}, {g.location.lng})\")\n"
    "         return g",
    1,
)
print("OK: added cache INSERT print in _one_geocode")

with open("src/or_engine/cli.py", "w", encoding="utf-8") as f:
    f.write(content)


# ── Fix travel.py: per-pair cache hit/miss/insert ────────────────────
with open("src/or_engine/spatial/travel.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Per-pair HIT and MISS in the scan loop
old_scan = '''    for i, j in pairs:
        row = await cache.get_travel_pair(points[i].to_str(), points[j].to_str(), mode)
        if row is not None:
            distance[i][j] = float(row["distance_m"] or 0.0)
            duration[i][j] = float(row["duration_s"] or 0.0)
            if symmetric:
                distance[j][i] = distance[i][j]
                duration[j][i] = duration[i][j]
            cache_hits += 1
        else:
            todo.append((i, j))'''

new_scan = '''    for i, j in pairs:
        from_ref = points[i].to_str()
        to_ref = points[j].to_str()
        row = await cache.get_travel_pair(from_ref, to_ref, mode)
        if row is not None:
            distance[i][j] = float(row["distance_m"] or 0.0)
            duration[i][j] = float(row["duration_s"] or 0.0)
            if symmetric:
                distance[j][i] = distance[i][j]
                duration[j][i] = duration[i][j]
            cache_hits += 1
            print(f"             [cache]  HIT   direction  "
                  f"{refs[i]} -> {refs[j]}  "
                  f"dist={distance[i][j]:.0f}m  dur={duration[i][j]:.0f}s")
        else:
            todo.append((i, j))
            print(f"             [cache]  MISS  direction  "
                  f"{refs[i]} -> {refs[j]}  (will call AMap)")'''

if old_scan in content:
    content = content.replace(old_scan, new_scan, 1)
    print("OK: travel.py per-pair HIT/MISS logging")
else:
    print("WARN: travel.py scan loop not found")

# 2. Per-pair INSERT in _compute_pairs_amap's _one function
old_save = '''             # Optionally write back to cache (sequential within lock)
            if cache is not None:
                await cache.save_travel_pair(
                    from_ref=points[i].to_str(),
                    to_ref=points[j].to_str(),
                    mode=mode,
                    distance_m=dm,
                    duration_s=ds,
                 )'''

new_save = '''             # Optionally write back to cache (sequential within lock)
            if cache is not None:
                await cache.save_travel_pair(
                    from_ref=points[i].to_str(),
                    to_ref=points[j].to_str(),
                    mode=mode,
                    distance_m=dm,
                    duration_s=ds,
                 )
                print(f"             [cache]  INSERT direction  "
                      f"{refs[i]} -> {refs[j]}  "
                      f"dist={dm:.0f}m  dur={ds:.0f}s")'''

if old_save in content:
    content = content.replace(old_save, new_save, 1)
    print("OK: travel.py per-pair INSERT logging")
else:
    print("WARN: travel.py _compute save pattern not found")

with open("src/or_engine/spatial/travel.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\nDone.")

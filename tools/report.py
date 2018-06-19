import argparse
import csv
import json

parser = argparse.ArgumentParser(description='Report stats from log file')
parser.add_argument('logfile', type=str)
parser.add_argument('outputdir', type=str)

args = parser.parse_args()
file_mapping = {}
CUMULATIVE = 'cumulative'
START_TOKEN = '#### Profiled '
END_TOKEN = '#### End ####'
TIME_HEADER = 'filename:lineno'
JSON_FILE = 'json.txt'


def report(logfile, outdir):

    with open(logfile) as f:
        fiter = iter(f)
        for line in fiter:
            try:
                idx = line.index(START_TOKEN)
            except ValueError:
                continue
            ms = idx + len(START_TOKEN)
            method = line[ms: line.index(' ', ms)]
            c_time = file_mapping.setdefault(CUMULATIVE, {}).setdefault(method,
                                                                        [])
            n = next(fiter)
            c_time.append(float(n.split()[-2]))
            # specific per function times
            s_time = file_mapping.setdefault(method, {})
            # Skip to the numbers
            while TIME_HEADER not in n:
                n = next(fiter)

            while END_TOKEN not in n:
                n = next(fiter)
                l = n.split()
                if len(l) < 6:
                    continue
                filefunc = l[-1]
                # Remove line number
                times = s_time.setdefault(filefunc[:filefunc.index(':')] +
                                          filefunc[filefunc.index('('):], [])
                times.append(float(l[3]))

    with open(outdir + JSON_FILE, "w") as f:
        f.write(json.dumps(file_mapping))

    for k, v in file_mapping.iteritems():
        fname = ['function']
        calls = [
            'call(%d)' % d for d in xrange(max(len(vv) for vv in v.values()))]

        with open(outdir + k + '.csv', 'w') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fname + calls)
            writer.writeheader()
            for fname, times in v.iteritems():
                d = {'function': fname}
                d.update({x: times[i] if i < len(times) else 0.0 for i, x in
                          enumerate(calls)})
                writer.writerow(d)


if __name__ == '__main__':
    report(args.logfile, args.outputdir)

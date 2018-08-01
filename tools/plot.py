# libraries and data
import argparse
import json
import math
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser(description='Plot stats from report')
parser.add_argument('reportfile', type=str)
parser.add_argument('function', type=str)
args = parser.parse_args()

reportfile = args.reportfile
func = args.function
report = {}

with open(reportfile, "r") as f:
    content = f.readlines()
    report.update(json.loads(content[0]))


freport = {k:v for k,v in report[func].iteritems() if max(v) > 0.01 and len(v) > 100}


nums = len(freport)
high = int(math.ceil(nums**(1/2.0)))
low = int(math.floor(nums**(1/2.0)))

if low * high < nums:
    low = high

rows = low
cols = high

# Initialize the figure
plt.style.use('seaborn-darkgrid')

# create a color palette
palette = plt.get_cmap('tab20')
 
num = 0
for k, v in freport.iteritems():
    num += 1

    # Find the right spot on the plot
    plt.subplot(rows, cols, num) 
 
    # Plot the lineplot
    plt.plot(range(1, len(v) + 1), v, marker='', color=palette(num),
             linewidth=2.4, alpha=0.9, label=k.split('/')[-1])

    maxx = len(v)
    maxy = max(v)
 
    plt.xlim(0, maxx)
    plt.ylim(0, maxy)

    # Add title
    plt.title(k.split('/')[-1], loc='left', fontsize=12, fontweight=0,
              color=palette(num) )

plt.show()

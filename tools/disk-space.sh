#!/bin/bash

# Infinite loop  
while true; do  
	# Display disk usage
	date "+%H:%M:%S   %d/%m/%y" >> disk-size-logs.txt
	df -h >> disk-size-logs.txt
	echo "---------------------------------------" >> disk-size-logs.txt
	  
	# Wait for 20 seconds  
	sleep 20  
done

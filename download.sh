#!/bin/bash

galleries=$(cat "$1" | wc -l)

i=1

while read -r line
do
	time ./nh2_requireCfToken.sh "$line" -c "csrftoken=iC1KsbsFEKBsjiZYu3FmNk1lC9EppTJU; cf_clearance=KDpNRPdyGQNZy4cgAI3_CqjGBLal4NFNe3qbxTgQA00-1739375345-1.2.1.1-2RYWJNuIELpAZivAYvlrbgUfZgebswoRjEo9_nmdcy9FRwAW_K5wsXpydce5Wx4a.Zn8mPEipV09q2DEcS9H0lpq_o9zCrRMXQUymDF1gcSCkWgfjCOU.2aceCOMhI1dUeRKi0plg29e5_M24k8JOsz3p1J4bh6iulOmAPX8PV3SEYj6yc0RdbVJRTFlZsT7BZKGrha6ysrLStyZYjLsq0V6xWxHIEtnHhFc6WXy78XJgqaiX0rvrjodzVSfZysWitg6faWLWYlUXjDimSs3otopr4mmsz29XJYrUtCM_pcSfvqp_j4gfUUTcobl8L.VvI9u4Vey_BuPDLkEfTKY3g; session-affinity=1748238898.412.52.578182|2968378f2272707dac237fc5e1f12aaf" -a "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0"
	echo "$i/$galleries done"
	i=$((i+1))
done < "$1"

#!/bin/bash

#set -x

export NITERS=${NITERS:=1}
export WORKERS=${WORKERS:="192.168.1.2 192.168.1.3 192.168.1.4"}
export SCHED=${SCHED:="default"}

mkdir -p $SCHED/results

for w in $WORKERS; do
    ssh $w "nohup perf stat -a -e power/energy-pkg/ -x, -I 1000 sleep 14400 > perf.log 2>&1 &"
done


for ((t=0;t<$NITERS;t++)); do    
    kubectl logs $(kubectl get pods | awk '/cilantroscheduler/ {print $1;exit}') > $SCHED/results/cilantroscheduler.log
    kubectl -c 'hr-client' logs $(kubectl get pods | awk '/hr-client/ {print $1;exit}') > $SCHED/results/hr-client.log
    kubectl -c 'cilantro-hr-client' logs $(kubectl get pods | awk '/hr-client/ {print $1;exit}') > $SCHED/results/cilantro-hr-client.log
    
    kubectl cp $(kubectl get pods | awk '/hr-client/ {print $1;exit}'):/cilantrologs $SCHED/results/cilantrologs
    kubectl cp $(kubectl get pods | awk '/cilantroscheduler/ {print $1;exit}'):/cilantro/workdirs $SCHED/results/workdirs

    kubectl get pods -o wide > $SCHED/results/kubectl.pods.$t
    kubectl get pods -o wide --all-namespaces > $SCHED/results/kubectl-all-namespaces.pods.$t
    
    echo "Sleeping for 1 hour ......"
    sleep 3600
    for w in $WORKERS; do
    	scp -r $w:~/perf.log $SCHED/results/perf.$w.log
    done
done

./clean_cluster.sh
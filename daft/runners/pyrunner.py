from __future__ import annotations

import copy
from bisect import bisect_right
from itertools import accumulate
from typing import Dict

from pyarrow import csv

from daft.execution.execution_plan import ExecutionPlan
from daft.logical.logical_plan import (
    Filter,
    GlobalLimit,
    LocalLimit,
    LogicalPlan,
    Projection,
    Repartition,
    Scan,
    Sort,
)
from daft.runners.partitioning import PartitionSet, vPartition
from daft.runners.runner import Runner
from daft.runners.shuffle_ops import RepartitionRandomOp, ShuffleOp, SortOp


class PyRunnerPartitionManager:
    def __init__(self) -> None:
        self._nid_to_partition_set: Dict[int, PartitionSet] = {}

    def put(self, node_id: int, partition_id: int, partition: vPartition) -> None:
        if node_id not in self._nid_to_partition_set:
            self._nid_to_partition_set[node_id] = PartitionSet({})

        pset = self._nid_to_partition_set[node_id]
        pset.partitions[partition_id] = partition

    def get(self, node_id: int, partition_id: int) -> vPartition:
        assert node_id in self._nid_to_partition_set
        pset = self._nid_to_partition_set[node_id]

        assert partition_id in pset.partitions
        return pset.partitions[partition_id]

    def get_partition_set(self, node_id: int) -> PartitionSet:
        assert node_id in self._nid_to_partition_set
        return self._nid_to_partition_set[node_id]

    def put_partition_set(self, node_id: int, pset: PartitionSet) -> None:
        self._nid_to_partition_set[node_id] = pset

    def rm(self, id: int):
        ...


class PyRunnerSimpleShuffler(ShuffleOp):
    def run(self, input: PartitionSet, num_target_partitions: int) -> PartitionSet:
        map_args = self._map_args if self._map_args is not None else {}
        reduce_args = self._reduce_args if self._reduce_args is not None else {}

        source_partitions = input.num_partitions()
        map_results = [
            self.map_fn(input=input.partitions[i], output_partitions=num_target_partitions, **map_args)
            for i in range(source_partitions)
        ]
        reduced_results = []
        for t in range(num_target_partitions):
            reduced_part = self.reduce_fn([map_results[i][t] for i in range(source_partitions)], **reduce_args)
            reduced_results.append(reduced_part)

        return PartitionSet({i: part for i, part in enumerate(reduced_results)})


class PyRunnerRepartitionRandom(PyRunnerSimpleShuffler, RepartitionRandomOp):
    ...


class PyRunnerSortOp(PyRunnerSimpleShuffler, SortOp):
    ...


class PyRunner(Runner):
    def __init__(self) -> None:
        self._part_manager = PyRunnerPartitionManager()

    def run(self, plan: LogicalPlan) -> PartitionSet:
        exec_plan = ExecutionPlan.plan_from_logical(plan)
        for exec_op in exec_plan.execution_ops:

            if exec_op.is_global_op:
                for node in exec_op.logical_ops:
                    if isinstance(node, GlobalLimit):
                        self._handle_global_limit(node)
                    elif isinstance(node, Repartition):
                        self._handle_repartition(node)
                    elif isinstance(node, Sort):
                        self._handle_sort(node)
                    else:
                        raise NotImplementedError(f"{type(node)} not implemented")
            else:
                for i in range(exec_op.num_partitions):
                    for node in exec_op.logical_ops:
                        if isinstance(node, Scan):
                            self._handle_scan(node, partition_id=i)
                        elif isinstance(node, Projection):
                            self._handle_projection(node, partition_id=i)
                        elif isinstance(node, Filter):
                            self._handle_filter(node, partition_id=i)
                        elif isinstance(node, LocalLimit):
                            self._handle_local_limit(node, partition_id=i)
                        else:
                            raise NotImplementedError(f"{type(node)} not implemented")
        return self._part_manager.get_partition_set(node.id())

    def _handle_scan(self, scan: Scan, partition_id: int) -> None:
        n_partitions = scan.num_partitions()
        assert n_partitions == 1
        assert partition_id == 0
        if scan._source_info.scan_type == Scan.ScanType.IN_MEMORY:
            assert n_partitions == 1
            raise NotImplementedError()
        elif scan._source_info.scan_type == Scan.ScanType.CSV:
            assert isinstance(scan._source_info.source, Scan.CSVScanConfig)
            schema = scan.schema()
            table = csv.read_csv(
                scan._source_info.source.path,
                parse_options=csv.ParseOptions(
                    delimiter=scan._source_info.source.delimiter,
                ),
                read_options=csv.ReadOptions(
                    column_names=[expr.name() for expr in schema],
                    skip_rows_after_names=1 if scan._source_info.source.headers else 0,
                ),
            )
            column_ids = [col.get_id() for col in schema.to_column_expressions()]
            vpart = vPartition.from_arrow_table(table, column_ids=column_ids, partition_id=partition_id)
            self._part_manager.put(scan.id(), partition_id=partition_id, partition=vpart)

    def _handle_projection(self, proj: Projection, partition_id: int) -> None:
        child_id = proj._children()[0].id()
        prev_partition = self._part_manager.get(child_id, partition_id)
        new_partition = prev_partition.eval_expression_list(proj._projection)
        self._part_manager.put(proj.id(), partition_id=partition_id, partition=new_partition)

    def _handle_filter(self, filter: Filter, partition_id: int) -> None:
        predicate = filter._predicate
        child_id = filter._children()[0].id()
        prev_partition = self._part_manager.get(child_id, partition_id)
        new_partition = prev_partition.filter(predicate)
        self._part_manager.put(filter.id(), partition_id=partition_id, partition=new_partition)

    def _handle_local_limit(self, limit: LocalLimit, partition_id: int) -> None:
        num = limit._num
        child_id = limit._children()[0].id()
        prev_partition = self._part_manager.get(child_id, partition_id)
        new_partition = prev_partition.head(num)
        self._part_manager.put(limit.id(), partition_id=partition_id, partition=new_partition)

    def _handle_global_limit(self, limit: GlobalLimit) -> None:
        num = limit._num
        child_id = limit._children()[0].id()
        prev_pset = self._part_manager.get_partition_set(child_id)
        new_pset = copy.copy(self._part_manager.get_partition_set(child_id))

        size_per_partition = prev_pset.len_of_partitions()
        total_size = sum(size_per_partition)
        if total_size <= num:
            self._part_manager.put_partition_set(limit.id(), prev_pset)
            return

        cum_sum = list(accumulate(size_per_partition))
        where_to_cut_idx = bisect_right(cum_sum, num)
        count_so_far = cum_sum[where_to_cut_idx - 1]
        remainder = num - count_so_far
        assert remainder >= 0
        new_pset.partitions[where_to_cut_idx] = new_pset.partitions[where_to_cut_idx].head(remainder)
        for i in range(where_to_cut_idx + 1, limit.num_partitions()):
            new_pset.partitions[i] = new_pset.partitions[i].head(0)
        self._part_manager.put_partition_set(limit.id(), new_pset)

    def _handle_repartition(self, repartition: Repartition) -> None:
        child_id = repartition._children()[0].id()
        prev_pset = self._part_manager.get_partition_set(child_id)
        repartitioner = PyRunnerRepartitionRandom()
        new_pset = repartitioner.run(input=prev_pset, num_target_partitions=repartition.num_partitions())
        self._part_manager.put_partition_set(repartition.id(), new_pset)

    def _handle_sort(self, sort: Sort) -> None:
        SAMPLES_PER_PARTITION = 20
        num_partitions = sort.num_partitions()
        child_id = sort._children()[0].id()
        prev_pset = self._part_manager.get_partition_set(child_id)
        sampled_partitions = [prev_pset.partitions[i].sample(SAMPLES_PER_PARTITION) for i in range(num_partitions)]
        merged_samples = vPartition.merge_partitions(sampled_partitions, verify_partition_id=False)
        assert len(sort._sort_by.exprs) == 1
        expr = sort._sort_by.exprs[0]
        sampled_sort_key = merged_samples.eval_expression(expr)
        boundaries = sampled_sort_key.block.bucket(num_partitions)

        sort_op = PyRunnerSortOp(
            map_args={"expr": expr, "boundaries": boundaries, "desc": sort._desc},
            reduce_args={"expr": expr, "desc": sort._desc},
        )
        new_pset = sort_op.run(input=prev_pset, num_target_partitions=num_partitions)
        self._part_manager.put_partition_set(sort.id(), new_pset)
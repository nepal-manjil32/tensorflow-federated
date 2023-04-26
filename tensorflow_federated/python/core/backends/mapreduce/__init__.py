# Copyright 2019, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Libraries for interacting with MapReduce-like backends.

This package contains libraries for using TFF in backend systems that offer
MapReduce-like capabilities, i.e., systems that can perform parallel processing
on a set of clients, and then aggregate the results of such processing on the
server. Systems of this type do not support the full expressiveness of TFF, but
they are common enough in practice to warrant a dedicated set of libraries, and
many examples of TFF computations, including those constructed by
`tff.learning`, can be compiled by TFF into a form that can be deployed on such
systems.

This package defines a few data structures: `BroadcastForm`, `MapReduceForm`,
and `DistributeAggregateForm`. `DistributeAggregateForm` will eventually replace
`BroadcastForm` and `MapReduceForm`.

The type signature of a TFF computation `round_comp` that can be converted into
`MapReduceForm` or `DistributeAggregateForm` is as follows:

```
(<S@SERVER,{D}@CLIENTS> -> <S@SERVER,X@SERVER>)
```

The server state is the first component of the input, and the computation
returns updated server state as the first component of the output. Since the
set of clients involved in a federated computation will (often) vary from round
to round, the server state is sometimes needed to connect subsequent rounds
into a single contiguous logical sequence. If there is no need for server
state, the input/output state should be modeled as an empty tuple. The
computation can also take client-side data as input, and can produce results
on server side in addition to state intended to be passed to the next round.
As is the case for the server state, if this is undesired it should be modeled
as an empty tuple.

The above type signature involves the following abstract types:

* `S` is the type of the state that is passed at the server between rounds of
  processing. For example, in the context of federated training, the server
  state would typically include the weights of the model being trained. The
  weights would be updated in each round as the model is trained on more and
  more of the clients' data, and hence the server state would evolve as well.

* `D` represents the type of per-client units of data that serve as the input
  to the computation. Often, this would be a sequence type, i.e., a dataset
  in TensorFlow's parlance, although strictly speaking this does not have to
  always be the case.

* `X` represents the type of server-side outputs generated by the server after
  each round.

One can think of the process based on this representation as being equivalent
to the following pseudocode loop:

```python
client_data = ...
server_state = initialize_comp()
while True:
  server_state, server_outputs = round_comp(server_state, client_data)
```

In `MapReduceForm`, the logic of `round_comp` is factored into seven main
components that are all TensorFlow functions: `prepare`, `work`, `zero`,
`accumulate`, `merge`, `report`, and `update`. There are also additional
`secure_sum_bitwidth`, `secure_sum_max_input`, and `secure_modular_sum_modulus`
TensorFlow function components that specify runtime parameters for
`federated_secure_sum_*` intrinsics). The pseudocode below uses common
syntactic shortcuts (such as implicit zipping) when showing how an instance of
`MapReduceForm` maps to a single federated round.

```python
@tff.federated_computation
def round_comp(server_state, client_data):

  # The server prepares an input to be broadcast to all clients that controls
  # what will happen in this round.

  client_input = (
    tff.federated_broadcast(tff.federated_map(prepare, server_state)))

  # The clients all independently do local work and produce updates, plus the
  # optional client-side outputs.

  client_updates = tff.federated_map(work, [client_data, client_input])

  # `client_updates` is a 4-tuple whose elements are passed to the following
  # intrinsics:
  #    1. `federated_aggregate`
  #    2. `federated_secure_sum_bitwidth`
  #    3. `federated_secure_sum`
  #    4. `federated_secure_modular_sum`
  # The intrinsics aggregate the updates across the system into a single global
  # update at the server.

  simple_agg = tff.federated_aggregate(
    client_updates[0], zero(), accumulate, merge, report))
  secure_aggs = [
    tff.federated_secure_sum_bitwidth(client_updates[1], bitwidth()),
    tff.federated_secure_sum(client_updates[2], max_input()),
    tff.federated_secure_modular_sum(client_updates[3], modulus())]

  global_update = [simple_agg] + secure_aggs

  # Finally, the server produces a new state as well as server-side output to
  # emit from this round.

  new_server_state, server_output = (
    tff.federated_map(update, [server_state, global_update]))

  # The updated server state, server- and client-side outputs are returned as
  # results of this round.

  return new_server_state, server_output
```

Details on the seven main pieces of pure TensorFlow logic in the `MapReduceForm`
are below. Please also consult the documentation for related federated operators
for more detail (particularly the `tff.federated_aggregate()`, as several of the
components below correspond directly to the parameters of that operator).

* `prepare` represents the preparatory steps taken by the server to generate
  inputs that will be broadcast to the clients and that, together with the
  client data, will drive the client-side work in this round. It takes the
  initial state of the server, and produces the input for use by the clients.
  Its type signature is `(S -> C)`.

* `work` represents the totality of client-side processing, again all as a
  single section of TensorFlow code. It takes a tuple of client data and
  client input that was broadcasted by the server, and returns a two-tuple
  containing the client update to be aggregated (across all the clients). The
  first index of this two-tuple will be passed to an aggregation parameterized
  by the blocks of TensorFlow below (`zero`, `accumulate`, `merge`, and
  `report`), and the second index will be passed to
  `federated_secure_sum_bitwidth`. Its type signature is `(<D,C> -> <U,V>)`.

* `zero` is the TensorFlow computation that produces the initial state of
  accumulators that are used to combine updates collected from subsets of the
  client population. In some systems, all accumulation may happen at the
  server, but for scalability reasons, it is often desirable to structure
  aggregation in multiple tiers. Its type signature is `A`, or when
  represented as a `tff.Computation` in Python, `( -> A)`.

* `accumulate` is the TensorFlow computation that updates the state of an
  update accumulator (initialized with `zero` above) with a single client's
  update. Its type signature is `(<A,U> -> A)`. Typically, a single accumulator
  would be used to combine the updates from multiple clients, but this does
  not have to be the case (it's up to the target deployment platform to choose
  how to use this logic in a particular deployment scenario).

* `merge` is the TensorFlow computation that merges two accumulators holding
  the results of aggregation over two disjoint subsets of clients. Its type
  signature is `(<A,A> -> A)`.

* `report` is the TensorFlow computation that transforms the state of the
  top-most accumulator (after accumulating updates from all clients and
  merging all the resulting accumulators into a single one at the top level
  of the system hierarchy) into the final result of aggregation. Its type
  signature is `(A -> R)`.

* `update` is the TensorFlow computation that applies the aggregate of all
  clients' updates (the output of `report`), also referred to above as the
  global update, to the server state, to produce a new server state to feed
  into the next round, and that additionally outputs a server-side output,
  to be reported externally as one of the results of this round. In federated
  learning scenarios, the server-side outputs might include things like loss
  and accuracy metrics, and the server state to be carried over, as noted
  above, may include the model weights to be trained further in a subsequent
  round. The type signature of this computation is `(<S,R> -> <S,X>)`.

The above TensorFlow computations' type signatures involves the following
abstract types in addition to those defined earlier:

* `C` is the type of the inputs for the clients, to be supplied by the server
  at the beginning of each round (or an empty tuple if not needed).

* `U` is the type of the per-client update to be produced in each round and
  fed into the cross-client federated aggregation protocol.

* `V` is the type of the per-client update to be produced in each round and
  fed into the cross-client secure aggregation protocol.

* `A` is the type of the accumulators used to combine updates from subsets of
  clients.

* `R` is the type of the final result of aggregating all client updates, the
  global update to be incorporated into the server state at the end of the
  round (and to produce the server-side output).


In `DistributeAggregateForm`, the logic of `round_comp` is factored into five
main components that are all TFF Lambda Computations (as defined in
`computation.proto`): `server_prepare`, `server_to_client_broadcast`,
`client_work`, `client_to_server_aggregation`, and `server_result`. The
pseudocode below shows how an instance of `DistributeAggregateForm` maps to a
single federated round.

```python
@tff.federated_computation
def round_comp(server_state, client_data):
  # The server prepares an input to be broadcast to all clients and generates
  # a temporary state that may be used by later parts of the computation.
  context_at_server, post_client_work_state = server_prepare(server_state)

  # Broadcast context_at_server to the clients.
  context_at_clients = server_to_client_broadcast(context_at_server)

  # The clients all independently do local work and produce updates.
  work_at_clients = client_work(client_data, context_at_clients)

  # Aggregate the client updates.
  intermediate_result_at_server = client_to_server_aggregation(
      post_client_work_state, work_at_clients)

  # Finally, the server produces a new state as well as server-side output to
  # emit from this round.
  new_server_state, server_output = server_result(
      post_client_work_state, intermediate_result_at_server)

  # The updated server state and server-side output are returned as results of
  # this round.
  return new_server_state, server_output
```

Details on the five components of `DistributeAggregateForm` are below.

* `server_prepare` represents the preparatory steps taken by the server to
  generate 1) inputs that will be broadcast to the clients and 2) a temporary
  state that may be needed by the `client_to_server_aggregation` and
  `server_result` components. The entire lambda may contain only SERVER
  placements, and its type signature is `(S -> <B_I, T>)`.

* `server_to_client_broadcast` represents the broadcast of data from the server
  to the clients. It contains a block of locals that are exclusively intrinsics
  with IntrinsicDef.broadcast_kind and that depend only on the
  `server_to_client_broadcast` args. It returns the results of these intrinsics
  in the order they are computed. Its type signature is `(B_I -> B_O)`.

* `client_work` represents the totality of client-side processing. It takes a
  tuple of client data and client input that was broadcasted by the server, and
  returns the client update to be aggregated (across all the clients). The
  entire lambda may contain only CLIENTS placements, and its type signature is
  `(<D, B_O> -> A_I)`.

* `client_to_server_aggregation` represents the aggregation of data from the
  clients to the server. It may incorporate the temporary state that was
  generated by the `server_prepare` component to set dynamic aggregation
  parameters. It contains a block of locals that are exclusively intrinsics with
  IntrinsicDef.aggregation_kind and that depend only on the
  `client_to_server_aggregation` args. It returns the results of these
  intrinsics in the order they are computed. Its type signature is
  `(<T, A_I> -> A_O)`.

* `server_result` represents the post-processing steps taken by the server.
  It may depend on the temporary state that was generated by the
  `server_prepare` component, and the data that was aggregated from the clients.
  It will generate a new server state to feed into the next round and an
  additional server-side output to be reported externally as one of the results
  of this round. In federated learning scenarios, the server-side outputs might
  include things like loss and accuracy metrics, and the server state to be
  carried over may include the model weights. The entire lambda may contain only
  SERVER placements, and its type signature is `(<T, A_O> -> <S, X>)`.

The above TFF Lambda Computations' type signatures involves the following
abstract types in addition to those defined earlier:

* `B_I` is the type of the broadcast inputs.

* `B_O` is the type of the broadcast outputs.

* `A_I` is the type of the aggregation inputs.

* `A_O` is the type of the aggregation outputs.

* `T` is the type of the temporary state.
"""

# TODO(b/138261370): Cover this in the general set of guidelines for deployment.

from tensorflow_federated.python.core.backends.mapreduce.compiler import consolidate_and_extract_local_processing
from tensorflow_federated.python.core.backends.mapreduce.compiler import parse_tff_to_tf
from tensorflow_federated.python.core.backends.mapreduce.form_utils import check_computation_compatible_with_map_reduce_form
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_broadcast_form_for_computation
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_computation_for_broadcast_form
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_computation_for_distribute_aggregate_form
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_computation_for_map_reduce_form
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_distribute_aggregate_form_for_computation
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_map_reduce_form_for_computation
from tensorflow_federated.python.core.backends.mapreduce.form_utils import get_state_initialization_computation
from tensorflow_federated.python.core.backends.mapreduce.forms import BroadcastForm
from tensorflow_federated.python.core.backends.mapreduce.forms import DistributeAggregateForm
from tensorflow_federated.python.core.backends.mapreduce.forms import MapReduceForm

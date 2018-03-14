import logging as log
import z3
import uuid
import json
import sys

STARTING_LEVEL_FEATURE_SPECULATIVE_PRUNING = 64
SPECULATIVE_PRUNING_TIMEOUT = 30000

def get_dic_of_features_to_check(optional_features):
    to_check = {}
    for i in optional_features:
        for k in optional_features[i]:
            for j in range(k[0],k[1]+1):
                if j in to_check:
                    to_check[j].append(i)
                else:
                    to_check[j] = [i]
    return to_check


def get_basic_formula_list(features, attributes, contexts, constraints, features_as_boolean):
    formulas = []
    if not features_as_boolean:
        for i in features:
            formulas.append(0 <= z3.Int(i))
            formulas.append(z3.Int(i) <= 1)
    for i in attributes.keys():
        formulas.append(attributes[i]["min"] <= z3.Int(i))
        formulas.append(z3.Int(i) <= attributes[i]["max"])
    for i in contexts.keys():
        formulas.append(contexts[i]["min"] <= z3.Int(i))
        formulas.append(z3.Int(i) <= contexts[i]["max"])
    for i in constraints:
        formulas.append(i)
    return formulas


def get_time_context(time_context, optional_features):
    if not time_context:
        time_context = "_" + uuid.uuid4().hex
        for i in optional_features:
            optional_features[i].append((0,0))
    return time_context


def get_fail_checks_from_model(dead_ls, false_ls, model, features_as_boolean):
    dead_remove = []
    false_remove = []
    if features_as_boolean:
        val_true = z3.BoolVal(True)
        val_zero = z3.BoolVal(False)
        for j in dead_ls:
            if model[z3.Bool(j)] == val_true:
                dead_remove.append(j)
        for j in false_ls:
            if model[z3.Bool(j)] == val_zero:
                false_remove.append(j)
    else:
        val1 = z3.IntVal(1)
        val0 = z3.IntVal(0)
        for j in dead_ls:
            if model[z3.Int(j)] == val1:
                dead_remove.append(j)
        for j in false_ls:
            if model[z3.Int(j)] == val0:
                false_remove.append(j)
    log.debug("Removed {} ({}) dead (false optional) checks".format(
        len(dead_remove), len(false_remove)))
    return dead_remove, false_remove


def run_feature_analysis_with_optimization(
        features,
        features_as_boolean,
        contexts,
        attributes,
        constraints,
        optional_features,
        non_incremental_solver,
        out_stream,
        time_context=""):
    """
    Performs the feature analysis task.
    Tries first to prune level features at the time with a given timeout.
    If the timeout expires then level is decreased
    When level reaches 1 than one of the possible dead features is checked using an or
    If found another more restricting or constraint is added, until all the dead features are found.
    """

    data = {"dead_features": {}, "false_optionals": {}}
    solver = z3.Solver()
    #solver = z3.Then('simplify', 'nla2bv', 'smt').solver()
    if non_incremental_solver:
        solver.set("combined_solver.solver2_timeout",1)

    # if time variable is not defined, create a fictional one
    time_context = get_time_context(time_context, optional_features)

    log.debug("Add basic constraints")
    solver.add(get_basic_formula_list(features, attributes, contexts, constraints,features_as_boolean))

    if not non_incremental_solver:
        log.debug("Preliminary check")
        solver.check()

    # list of the features to check
    to_check = get_dic_of_features_to_check(optional_features)
    to_check_dead = {i: set(to_check[i]) for i in to_check}
    to_check_false = {i: set(to_check[i]) for i in to_check}

    log.info("Features to check: {}, Time context".format(
        len(optional_features), len(to_check)))

    for i in to_check_dead:
        log.debug("Processing time instant {}, features to check {}".format(i,len(to_check_dead[i])))
        solver.push()
        solver.add(z3.Int(time_context).__eq__(z3.IntVal(i)))

        if not non_incremental_solver:
            log.debug("Preliminary check")
            solver.check()

        solver.push()

        log.debug("Checking for dead features")
        limit = STARTING_LEVEL_FEATURE_SPECULATIVE_PRUNING
        all_in_once = max(len(to_check_dead[i])/2,1)
        all_in_once = min(limit,all_in_once)

        while to_check_dead[i]:

            log.debug("{} ({}) dead (false optional) features to check".format(
                len(to_check_dead[i]), len(to_check_false[i])))

            if all_in_once == 1:
                solver.set('smt.timeout',4294967295)
                if features_as_boolean:
                    solver.add(z3.Or([z3.Bool(j) for j in to_check_dead[i]]))
                else:
                    solver.add(z3.Or([z3.Int(j).__eq__(z3.IntVal(1)) for j in to_check_dead[i]]))
            else:
                solver.push()
                solver.set('smt.timeout', SPECULATIVE_PRUNING_TIMEOUT)
                log.debug("Attempt to prune {} features at once".format(all_in_once))
                if features_as_boolean:
                    solver.add(z3.PbGe([(z3.Bool(j), 1) for j in to_check_dead[i]], all_in_once))
                else:
                    solver.add(z3.PbGe([(z3.Int(j).__eq__(z3.IntVal(1)), 1) for j in to_check_dead[i]], all_in_once))

            result = solver.check()
            log.debug("Solver result {}".format(result))
            if result == z3.unsat:
                if all_in_once == 1:
                    to_check_false[i].difference_update(to_check_dead[i])
                    for j in to_check_dead[i]:
                        if j in data["dead_features"]:
                            data["dead_features"][j].append(i)
                        else:
                            data["dead_features"][j] = [i]
                    break
                else:
                    solver.pop()
                    all_in_once = max(all_in_once/2, 1)
            elif result == z3.sat:
                to_remove_dead, to_remove_false = get_fail_checks_from_model(
                    to_check_dead[i], to_check_false[i], solver.model(), features_as_boolean)
                to_check_dead[i].difference_update(to_remove_dead)
                to_check_false[i].difference_update(to_remove_false)

                if all_in_once != 1:
                    solver.pop()
                all_in_once = max(min(all_in_once,len(to_check_dead[i]) / 2), 1)
                all_in_once = min(limit, all_in_once)
            else:
                log.debug("Execution not terminated without the timeout. Moving on")
                solver.pop()
                all_in_once = max(all_in_once / 2, 1)

        solver.pop()
        solver.push()

        log.debug("Checking for false optional features")
        while to_check_false[i]:
            log.debug("{} false optional features to check".format(len(to_check_false[i])))
            if features_as_boolean:
                solver.add(z3.Or([z3.Not(z3.Bool(j)) for j in to_check_false[i]]))
            else:
                solver.add(z3.Or([z3.Int(j).__eq__(z3.IntVal(0)) for j in to_check_false[i]]))
            result = solver.check()
            if result == z3.unsat:
                for j in to_check_false[i]:
                    if j in data["false_optionals"]:
                        data["false_optionals"][j].append(i)
                    else:
                        data["false_optionals"][j]= [i]
                break
            elif result == z3.sat:
                _, to_remove_false = get_fail_checks_from_model(
                    [], to_check_false[i], solver.model(), features_as_boolean)
                to_check_false[i].difference_update(to_remove_false)
        solver.pop()
        solver.pop()

    log.info("Printing output")
    json.dump(data, out_stream)
    out_stream.write("\n")


def run_feature_analysis_grid_search(
        features,
        features_as_boolean,
        contexts,
        attributes,
        constraints,
        optional_features,
        non_incremental_solver,
        out_stream,
        time_context=""):
    """
    Performs the feature analysis one feature at the time with push and pops. Time context is set to all its values
    in sequence.
    Does not check the model except the first time for pruning the remaining features.
    This helps for big instances where generating the model make take some time.
    """

    data = {"dead_features": {}, "false_optionals": {}}
    solver = z3.Solver()
    if non_incremental_solver:
        solver.set("combined_solver.solver2_timeout",1)

    # if time variable is not defined, create a fictional one
    time_context = get_time_context(time_context, optional_features)

    log.debug("Add basic constraints")
    solver.add(get_basic_formula_list(features, attributes, contexts, constraints, features_as_boolean))

    if not non_incremental_solver:
        log.debug("Preliminary check")
        solver.check()

    # list of the features to check
    to_check = get_dic_of_features_to_check(optional_features)
    to_check_dead = {i: set(to_check[i]) for i in to_check}
    to_check_false = {i: set(to_check[i]) for i in to_check}
    log.info("Features to check: {}, Time context {}".format(
        len(optional_features), len(to_check)))

    for i in to_check_dead:
        log.debug("Processing time instant {}, features to check {}".format(i,len(to_check_dead[i])))
        solver.push()
        solver.add(z3.Int(time_context).__eq__(z3.IntVal(i)))

        # run first time to prune easy features and check satisfiability
        result = solver.check()
        if result == z3.unsat:
            log.debug("All instances are dead for time {}".format(i))
            for j in to_check_dead[i]:
                if j in data["dead_features"]:
                    data["dead_features"][j].append(i)
                else:
                    data["dead_features"][j] = [i]
            continue
        elif result == z3.sat:
            to_remove_dead, to_remove_false = get_fail_checks_from_model(
                to_check_dead[i], to_check_false[i], solver.model(), features_as_boolean)
            to_check_dead[i].difference_update(to_remove_dead)
            to_check_false[i].difference_update(to_remove_false)
        else:
            log.debug("Problems in detecting the satisfiability of the instance. Z3 returned {}".format(result))
            sys.exit(1)

        log.debug("Checking for dead features")
        counter = len(to_check_dead[i])
        for j in to_check_dead[i]:
            log.debug("Processing feature {}, remaining {}".format(j, counter))
            counter -= 1
            solver.push()
            if features_as_boolean:
                solver.add(z3.Bool(j))
            else:
                solver.add(z3.Int(j).__eq__(z3.IntVal(1)))
            result = solver.check()
            log.debug("Result {}".format(result))
            if result == z3.unsat:
                log.debug("{} is a dead feature".format(j))
                if j in data["dead_features"]:
                    data["dead_features"][j].append(i)
                else:
                    data["dead_features"][j] = [i]
                to_check_false[i].discard(j)
            elif result != z3.sat:
                log.debug("Problems checking feature{} at time {}. Z3 returned {}".format(
                    j, i, result))
                sys.exit(1)
            solver.pop()

        log.debug("Checking for false optional features")
        counter = len(to_check_false[i])
        for j in to_check_false[i]:
            log.debug("Processing feature {}, remaining".format(j, counter))
            counter -= 1
            solver.push()
            if features_as_boolean:
                solver.add(z3.Not(z3.Bool(j)))
            else:
                solver.add(z3.Int(j).__eq__(z3.IntVal(0)))
            result = solver.check()
            log.debug("Result {}".format(result))
            if result == z3.unsat:
                log.debug("{} is a false optional feature".format(j))
                if j in data["false_optionals"]:
                    data["false_optionals"][j].append(i)
                else:
                    data["false_optionals"][j] = [i]
            elif result != z3.sat:
                log.debug("Problems checking feature{} at time {}. Z3 returned {}".format(
                    j, i, result))
                sys.exit(1)
            solver.pop()
        solver.pop()

    log.info("Printing output")
    json.dump(data, out_stream)
    out_stream.write("\n")


def run_feature_analysis_forall(
        features,
        features_as_boolean,
        contexts,
        attributes,
        constraints,
        optional_features,
        non_incremental_solver,
        out_stream,
        time_context=""):
    """
    Performs the feature analysis task.
    A quantifier formula is solved to detect the anomalies
    """

    data = {"dead_features": {}, "false_optionals": {}}
    solver = z3.Solver()
    #solver.set("smt.relevancy", 0)
    if non_incremental_solver:
        solver.set("combined_solver.solver2_timeout",1)

    # if time variable is not defined, create a fictional one
    time_context = get_time_context(time_context, optional_features)
    # add it in context if not present
    if time_context not in contexts:
        contexts[time_context]['min'] = 0
        contexts[time_context]['max'] = 0

    log.info("Building the FM formula")
    formulas = get_basic_formula_list(features, attributes, contexts, constraints, features_as_boolean)

    if not non_incremental_solver:
        log.debug("Preliminary check")
        solver.check()

    # list of the features to check
    to_check_dic = get_dic_of_features_to_check(optional_features)
    to_check = [(i,j) for i in to_check_dic for j in to_check_dic[i]]
    #log.debug(unicode(solver))

    log.info("Computing dead or false optional features considering {} optional features, {} possibilities".format(
        len(optional_features), len(to_check)))

    # update bounds of fresh variable
    fresh_var = "_" + uuid.uuid4().hex
    solver.add(0 <= z3.Int(fresh_var))
    solver.add(z3.Int(fresh_var) < z3.IntVal(len(to_check)))

    if features_as_boolean:
        z3_features = [z3.Bool(j) for j in features]
    else:
        z3_features = [z3.Int(j) for j in features]

    solver.push()

    log.info("Search for dead features")
    # exist d . for all features/attributes f_d = 0 \/ not FD
    solver.add(
        z3.ForAll(z3_features + [z3.Int(j) for j in attributes.keys()] + [z3.Int(j) for j in contexts],
                  z3.Implies(
                      z3.And([z3.Implies(z3.Int(fresh_var).__eq__(z3.IntVal(i)),
                                         z3.And([z3.Bool(to_check[i][1]) if features_as_boolean else
                                                 z3.Int(to_check[i][1]).__eq__(z3.IntVal(1)),
                                                 z3.Int(time_context).__eq__(z3.IntVal(to_check[i][0]))]))
                              for i in range(len(to_check))]),
                      z3.Not(z3.And(formulas)))))
    #log.debug(unicode(solver))

    while True:
        log.info("Computing")
        result = solver.check()

        if result == z3.sat:
            model = solver.model()
            value = model[z3.Int(fresh_var)].as_long()
            found_context = to_check[value][0]
            found_feature = to_check[value][1]
            log.debug("Dead feature for time {}: {}".format(found_context, found_feature))
            if found_feature in data["dead_features"]:
                data["dead_features"][found_feature].append(found_context)
            else:
                data["dead_features"][found_feature] = [found_context]
            # add constraint for next iteration
            solver.add(z3.Int(fresh_var).__ne__(z3.IntVal(value)))
        else:
            log.debug("Formula found unsat. No more dead features.")
            break
    solver.pop()

    log.info("Search for false positive features")
    solver.add(
        z3.ForAll(z3_features + [z3.Int(j) for j in attributes.keys()] + [z3.Int(j) for j in contexts],
                  z3.Implies(
                      z3.And([z3.Implies(z3.Int(fresh_var).__eq__(z3.IntVal(i)),
                                         z3.And([z3.Not(z3.Bool(to_check[i][1])) if features_as_boolean else
                                                 z3.Int(to_check[i][1]).__eq__(z3.IntVal(0)),
                                                 z3.Int(time_context).__eq__(z3.IntVal(to_check[i][0]))]))
                              for i in range(len(to_check))]),
                      z3.Not(z3.And(formulas)))))
    #log.debug(unicode(solver))

    while True:
        log.info("Computing")
        result = solver.check()

        if result == z3.sat:
            model = solver.model()
            value = model[z3.Int(fresh_var)].as_long()
            found_context = to_check[value][0]
            found_feature = to_check[value][1]
            log.debug("False positive feature for time {}: {}".format(found_context, found_feature))
            if found_feature in data["false_optionals"]:
                data["false_optionals"][found_feature].append(found_context)
            else:
                data["false_optionals"][found_feature] = [found_context]
            # add constraint for next iteration
            solver.add(z3.Int(fresh_var).__ne__(z3.IntVal(value)))
        else:
            log.debug("Formula found unsat. No more false positives.")
            break


    log.info("Printing output")
    json.dump(data, out_stream)
    out_stream.write("\n")

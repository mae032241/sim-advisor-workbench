/* Bounded sequential entry point; all getter traversal lives in ReadMph. */
import java.lang.reflect.Array;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public final class ReadMphBatch {
  static double elapsed(long start) { return (System.nanoTime()-start)/1e9; }
  static void write(String target, Object value) throws Exception {
    Path path=Paths.get(target), staged=Paths.get(target+".tmp");
    Files.write(staged,ReadMph.json(value).getBytes(StandardCharsets.UTF_8));
    Files.move(staged,path,StandardCopyOption.REPLACE_EXISTING);
  }
  static TimerTask deadline(Timer timer, long milliseconds, String phase) {
    TimerTask task=new TimerTask(){public void run(){System.err.println("MPH_BATCH_TIMEOUT "+phase);System.exit(124);}};
    timer.schedule(task,milliseconds);return task;
  }
  static void reset() {
    ReadMph.nodes.clear();ReadMph.warnings.clear();ReadMph.visited.clear();
    ReadMph.failureReasons.clear();ReadMph.truncationReasons.clear();ReadMph.propertyCounts.clear();
    ReadMph.propertyErrors=ReadMph.selectionErrors=ReadMph.enumerationErrors=0;
    ReadMph.propertyTotal=ReadMph.propertyObserved=ReadMph.propertyPartial=ReadMph.nonFinite=0;
    ReadMph.selectionTotal=ReadMph.selectionFields=ReadMph.selectionObserved=ReadMph.unsupportedSelectionGetters=0;
    ReadMph.unsupportedSelectionAccessors=ReadMph.propertyDeclared=0;ReadMph.truncated=false;
  }
  static Map<String,Object> snapshot(Class<?> util, Object model, String exportStatus) {
    return ReadMph.map("schema","mph-api-state/v1","runtime_version",ReadMph.text(ReadMph.optional(util,"getComsolVersion")),
      "file_saved_version",ReadMph.text(ReadMph.optional(model,"getComsolVersion")),"nodes",ReadMph.nodes,"warnings",ReadMph.warnings,
      "read_errors",ReadMph.propertyErrors+ReadMph.selectionErrors+ReadMph.enumerationErrors,"truncated",ReadMph.truncated,
      "read_contract","mph-api-read/v2",
      "read_coverage",ReadMph.map("properties",ReadMph.map("total",ReadMph.propertyTotal,"declared",ReadMph.propertyDeclared,"observed",ReadMph.propertyObserved,"partial",ReadMph.propertyPartial,"unresolved",ReadMph.propertyErrors),
        "selections",ReadMph.map("total",ReadMph.selectionTotal,"fields_attempted",ReadMph.selectionFields,"fields_observed",ReadMph.selectionObserved,"unresolved",ReadMph.selectionErrors,"unsupported_getters",ReadMph.unsupportedSelectionGetters,"unsupported_accessors",ReadMph.unsupportedSelectionAccessors),
        "traversal",ReadMph.map("unresolved",ReadMph.enumerationErrors),"non_finite_values",ReadMph.nonFinite,"failure_reasons",ReadMph.failureReasons),
      "limits",ReadMph.map("max_nodes",ReadMph.MAX_NODES,"max_properties_per_node",ReadMph.MAX_PROPERTIES,"max_array_items",ReadMph.MAX_ARRAY,"max_selection_objects",200),"truncation_reasons",ReadMph.truncationReasons,
      "export_status",exportStatus,"geometry_run",false,"mesh_run",false,"solver_run",false,"defaults_generated",false,"physical_validation","not_run");
  }
  public static void main(String[] args) {
    Timer watchdog=new Timer(true);List<Object> cases=new ArrayList<>();Class<?> util=null;
    long start=System.nanoTime();String manifest=args.length>1?args[1]:null;
    try {
      if(args.length<6 || (args.length-2)%4!=0 || (args.length-2)/4>5)throw new IllegalArgumentException("Expected deadline,manifest and at most five input,state,error,export groups");
      long timeout=Long.parseLong(args[0]);if(timeout<15000 || timeout>240000)throw new IllegalArgumentException("Case deadline is outside 15..240 seconds");
      TimerTask initDeadline=deadline(watchdog,timeout,"initialize");long stamp=System.nanoTime();
      util=Class.forName("com.comsol.model.util.ModelUtil");ReadMph.call(util,"initStandalone",false);ReadMph.optional(util,"showProgress",false);
      double initialize=elapsed(stamp);initDeadline.cancel();int count=(args.length-2)/4;
      for(int index=0;index<count;index++) {
        String input=args[2+index*4],state=args[3+index*4],errorPath=args[4+index*4],export=args[5+index*4];
        reset();TimerTask caseDeadline=deadline(watchdog,timeout,"case-"+index);long caseStart=System.nanoTime();
        Map<String,Object> timing=ReadMph.map("case_index",index);Throwable failure=null;boolean clean=true;Object model=null;
        try {
          stamp=System.nanoTime();model=ReadMph.call(util,"loadCopy","LibraryExtract",input);timing.put("load_seconds",elapsed(stamp));
          stamp=System.nanoTime();
          for(String name:new String[]{"component","study","sol","material","selection","variable","func","cpl"})ReadMph.branch(model,"model",name,0);
          Object result=ReadMph.optional(model,"result");if(result!=null)ReadMph.walk(result,"model/result","result",0);
          timing.put("getters_seconds",elapsed(stamp));stamp=System.nanoTime();String exportStatus="not_requested";
          try{ReadMph.call(model,"save",export,"java");exportStatus="exported_reconstruction_not_executed";}catch(Exception e){exportStatus="failed";ReadMph.warnings.add("Java export failed: "+e.getClass().getSimpleName());}
          timing.put("export_seconds",elapsed(stamp));stamp=System.nanoTime();write(state,snapshot(util,model,exportStatus));timing.put("serialize_seconds",elapsed(stamp));
        }catch(Throwable error){failure=error;}
        finally {
          stamp=System.nanoTime();
          try {
            if(model!=null)ReadMph.call(util,"remove","LibraryExtract");
            ReadMph.call(util,"clear");Object tags=ReadMph.call(util,"tags");int remaining=Array.getLength(tags);
            timing.put("remaining_model_tags",remaining);if(remaining!=0)throw new IllegalStateException("Model tags remain after cleanup");
          }catch(Throwable error){clean=false;if(failure==null)failure=error;}
          timing.put("remove_clear_seconds",elapsed(stamp));caseDeadline.cancel();
        }
        timing.put("case_seconds",elapsed(caseStart));timing.put("api_nodes",ReadMph.nodes.size());timing.put("read_errors",ReadMph.propertyErrors+ReadMph.selectionErrors+ReadMph.enumerationErrors);
        if(failure!=null) {
          Files.deleteIfExists(Paths.get(state));String message=String.valueOf(failure.getMessage());if(message.length()>1000)message=message.substring(0,1000);
          timing.put("status","failed");timing.put("error_class",failure.getClass().getName());timing.put("reason",message);write(errorPath,timing);
        }else timing.put("status","completed");
        cases.add(timing);write(manifest,ReadMph.map("schema","mph-api-batch-runtime/v1","initialize_seconds",initialize,"elapsed_seconds",elapsed(start),"cases",cases));
        System.out.println("MPH_BATCH_CASE "+ReadMph.json(timing));
        if(!clean)throw new IllegalStateException("Model cleanup failed; subsequent cases were not attempted");
      }
      watchdog.cancel();System.exit(0);
    }catch(Throwable error){System.err.println("MPH_BATCH_FAILED "+error.getClass().getName()+": "+String.valueOf(error.getMessage()));error.printStackTrace();System.exit(2);}
  }
}

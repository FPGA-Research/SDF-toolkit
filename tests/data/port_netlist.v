// Fixture for PORT->INTERCONNECT conversion.
//
// An untimed buffer feeds an AND gate, which feeds an inverter. The buffer has
// no IOPATH in the companion SDF, so its driving pin must be found by
// elimination. Primitives are blackboxes so Yosys keeps the instances and their
// bit-level connectivity while still knowing the top-level port directions.

(* blackbox *)
module BUF(input a, output y);
endmodule

(* blackbox *)
module AND2(input i1, input i2, output z);
endmodule

(* blackbox *)
module INV(input i, output z);
endmodule

module port_top(input a, input b, output y);
    wire x_buf;
    wire x_and;
    BUF  u_buf (.a(a),      .y(x_buf));
    AND2 u_and (.i1(x_buf), .i2(b), .z(x_and));
    INV  u_inv (.i(x_and),  .z(y));
endmodule
